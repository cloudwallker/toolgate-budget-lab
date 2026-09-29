"""A reclaimed reservation must never cross the HTTP send boundary."""

from uuid import uuid4

import httpx

from toolgate.budget import BudgetStore
from toolgate.contracts import CallRequest
from toolgate.execution import execute_call


async def test_reclaimed_reservation_cannot_start_tool(database, principal):
    store = BudgetStore(database)
    request = CallRequest(tool="catalog.lookup", arguments={"sku": "A"})
    call = (await store.admit(principal, uuid4(), request)).call
    assert await store.abort_reserved(call.id)

    def forbidden_send(request):
        raise AssertionError("aborted reservation must never reach a tool")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_send)) as client:
        result = await execute_call(
            store, client, "http://tools", "test-internal", call.id, request
        )
    assert result.state == "aborted" and not result.slot_held
    assert (await store.budget(principal))["used"] == 1
