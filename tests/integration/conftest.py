import os
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import delete

from toolgate.db import Database
from toolgate.models import AuditEvent, Call, DailyUsage, Principal


@pytest_asyncio.fixture
async def database():
    url = os.environ.get("TOOLGATE_TEST_DATABASE_URL")
    if not url:
        raise RuntimeError("TOOLGATE_TEST_DATABASE_URL is required; use scripts/test.ps1")
    database = Database(url, test_database=True)
    await database.initialize()
    yield database
    await database.close()


@pytest_asyncio.fixture
async def principal(database):
    identity = "test-" + uuid4().hex
    async with database.sessions.begin() as session:
        session.add(
            Principal(id=identity, allowed_tools=["catalog.lookup"], daily_limit=3, max_inflight=2)
        )
    yield identity
    async with database.sessions.begin() as session:
        for model in [AuditEvent, Call, DailyUsage]:
            await session.execute(delete(model).where(model.principal_id == identity))
        await session.execute(delete(Principal).where(Principal.id == identity))
