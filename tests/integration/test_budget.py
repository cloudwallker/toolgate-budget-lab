"""Real PostgreSQL contention: losing atomicity must fail these assertions."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select

from toolgate.budget import BudgetStore
from toolgate.contracts import CallRequest, ToolEvidence
from toolgate.models import AuditEvent, Call, DailyUsage, Principal


def request(sku="A"):
    return CallRequest(tool="catalog.lookup", arguments={"sku": sku})


async def test_last_daily_credit_cannot_be_admitted_twice(database, principal):
    async with database.sessions.begin() as session:
        row = await session.get(Principal, principal)
        row.daily_limit, row.max_inflight = 1, 30
    store = BudgetStore(database)
    arrivals = asyncio.Event()

    async def compete():
        await arrivals.wait()
        return await store.admit(principal, uuid4(), request())

    jobs = [asyncio.create_task(compete()) for _ in range(20)]
    arrivals.set()
    results = await asyncio.gather(*jobs)
    assert sum(result.is_new for result in results) == 1
    assert sum(result.error == "DAILY_BUDGET_EXHAUSTED" for result in results) == 19
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(func.sum(DailyUsage.used)).where(DailyUsage.principal_id == principal)
            )
            == 1
        )


async def test_concurrency_slot_and_idempotency_are_shared_between_store_instances(
    database, principal
):
    stores = [BudgetStore(database), BudgetStore(database)]
    results = await asyncio.gather(
        *[stores[index % 2].admit(principal, uuid4(), request()) for index in range(20)]
    )
    admitted = [result for result in results if result.is_new]
    assert len(admitted) == 2
    assert sum(result.error == "CONCURRENCY_EXHAUSTED" for result in results) == 18
    replay = await stores[1].admit(principal, admitted[0].call.request_key, request())
    assert replay.call.id == admitted[0].call.id
    assert not replay.is_new and replay.error is None
    conflict = await stores[1].admit(principal, admitted[0].call.request_key, request("B"))
    assert conflict.error == "IDEMPOTENCY_CONFLICT"


async def test_reserved_abort_competes_with_dispatch_and_daily_credit_is_not_refunded(
    database, principal
):
    store = BudgetStore(database)
    admission = await store.admit(principal, uuid4(), request())
    call_id = admission.call.id
    dispatched, aborted = await asyncio.gather(
        store.claim_dispatch(call_id), store.abort_reserved(call_id)
    )
    assert int(dispatched) + int(aborted) == 1
    current = await store.get_call(call_id, principal)
    assert current.slot_held == bool(dispatched)
    assert (await store.budget(principal))["used"] == 1


async def test_unknown_holds_slot_and_terminal_evidence_releases_only_once(database, principal):
    store = BudgetStore(database)
    admitted = await store.admit(principal, uuid4(), request())
    call_id = admitted.call.id
    assert await store.claim_dispatch(call_id)
    await store.mark_unknown(call_id, "RESPONSE_LOST")
    assert (await store.get_call(call_id, principal)).slot_held
    wrong = ToolEvidence(operation_id=UUID(int=1), state="succeeded", result={"price": 10})
    assert not await store.finalize(call_id, wrong)
    evidence = ToolEvidence(operation_id=call_id, state="succeeded", result={"price": 10})
    released = await asyncio.gather(
        store.finalize(call_id, evidence), store.finalize(call_id, evidence)
    )
    assert sum(released) == 1
    final = await store.get_call(call_id, principal)
    assert final.state == "succeeded" and not final.slot_held
    async with database.sessions() as session:
        events = await session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.call_id == call_id, AuditEvent.event == "RELEASED")
        )
    assert events == 1


async def test_day_rollover_does_not_reset_old_unknown_occupancy(database, principal):
    old = BudgetStore(database, clock=lambda: datetime(2026, 9, 29, 23, 59, tzinfo=UTC))
    new = BudgetStore(database, clock=lambda: datetime(2026, 9, 30, 0, 1, tzinfo=UTC))
    first = await old.admit(principal, uuid4(), request())
    assert await old.claim_dispatch(first.call.id)
    await old.mark_unknown(first.call.id, "TIMEOUT")
    second = await new.admit(principal, uuid4(), request())
    denied = await new.admit(principal, uuid4(), request())
    assert second.is_new and denied.error == "CONCURRENCY_EXHAUSTED"
    assert (await new.budget(principal))["used"] == 1
    assert (await new.budget(principal))["inflight"] == 2


async def test_authorization_rejection_has_no_call_or_credit(database, principal):
    store = BudgetStore(database)
    denied = await store.admit(
        principal, uuid4(), CallRequest(tool="catalog.summary", arguments={"sku": "A"})
    )
    assert denied.error == "TOOL_DENIED"
    assert (await store.budget(principal))["used"] == 0
    async with database.sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(Call).where(Call.principal_id == principal)
            )
            == 0
        )
