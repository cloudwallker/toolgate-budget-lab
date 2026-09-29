"""A real absent operation or unreachable tool process is not terminal evidence."""

from uuid import uuid4

import pytest
from test_process_lab import load_lab

from toolgate.budget import BudgetStore
from toolgate.contracts import CallRequest


@pytest.mark.parametrize("unreachable", [False, True])
async def test_readonly_reconcile_cannot_release_without_evidence(unreachable):
    async with load_lab().ProcessLab(daily=3, slots=1) as lab:
        store = BudgetStore(lab.database)
        # A dispatch commit may have succeeded without the sender observing it.
        call = (
            await store.admit(
                lab.principal,
                uuid4(),
                CallRequest(tool="catalog.lookup", arguments={"sku": "SKU-001"}),
            )
        ).call
        assert await store.claim_dispatch(call.id)
        if unreachable:
            process = lab.processes["tools"]
            process.kill()
            process.wait(timeout=5)
        assert not await lab.reconcile(call.id)
        current = await store.get_call(call.id)
        assert current.state == "dispatching" and current.slot_held
        assert (await lab.budget())["inflight"] == 1
        assert (await lab.budget())["used"] == 1
        assert not list(lab.directory.glob("started-*"))
