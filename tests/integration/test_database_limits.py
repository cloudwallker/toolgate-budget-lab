"""The real database enforces schema isolation and finite principal lock waits."""

import os
from time import perf_counter

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from mock_tools.store import OperationStore
from toolgate.budget import BudgetStore
from toolgate.contracts import CallRequest
from toolgate.models import Principal


async def test_schema_roles_cannot_access_each_others_records(database):
    url = os.environ["MOCK_TOOLS_TEST_DATABASE_URL"]
    assert (make_url(url).database or "").endswith("_test")
    tools = OperationStore(url)
    try:
        for store, own, other in ((database, "gateway", "tools"), (tools, "tools", "gateway")):
            async with store.sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT has_schema_privilege(current_user, :own, 'USAGE'), "
                            "has_schema_privilege(current_user, :own, 'CREATE'), "
                            "has_schema_privilege(current_user, :other, 'USAGE'), "
                            "has_schema_privilege(current_user, :other, 'CREATE')"
                        ),
                        {"own": own, "other": other},
                    )
                ).one()
                assert tuple(row) == (True, True, False, False)
    finally:
        await tools.close()


async def test_lock_wait_timeout_fails_without_admitting(database, principal):
    from uuid import uuid4

    store = BudgetStore(database)
    async with database.sessions.begin() as holder:
        await holder.scalar(select(Principal).where(Principal.id == principal).with_for_update())
        started = perf_counter()
        with pytest.raises(DBAPIError):
            await store.admit(
                principal, uuid4(), CallRequest(tool="catalog.lookup", arguments={"sku": "A"})
            )
        assert 1 <= perf_counter() - started < 6
    assert (await store.budget(principal))["used"] == 0
    assert (await store.budget(principal))["inflight"] == 0
