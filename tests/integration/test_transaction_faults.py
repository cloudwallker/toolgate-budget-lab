"""Inject faults at real PostgreSQL audit/commit boundaries, not an in-memory store."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from toolgate.budget import BudgetStore
from toolgate.contracts import CallRequest, ToolEvidence
from toolgate.models import AuditEvent, Call, Principal


def request():
    return CallRequest(tool="catalog.lookup", arguments={"sku": "A"})


async def test_audit_failure_rolls_back_admission_credit_and_call(database, principal):
    # Unique identifiers are generated here, never derived from external input.
    suffix = uuid4().hex
    function = f"reject_audit_{suffix}"
    trigger = f"reject_audit_{suffix}"
    async with database.sessions.begin() as session:
        await session.execute(
            text(f"""
            CREATE FUNCTION gateway.{function}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.principal_id = '{principal}' AND NEW.event = 'ADMITTED' THEN
                RAISE EXCEPTION 'injected audit failure';
              END IF;
              RETURN NEW;
            END $$
        """)
        )
        await session.execute(
            text(f"""
            CREATE TRIGGER {trigger} BEFORE INSERT ON gateway.audit_events
            FOR EACH ROW EXECUTE FUNCTION gateway.{function}()
        """)
        )
    try:
        store = BudgetStore(database)
        with pytest.raises(DBAPIError):
            await store.admit(principal, uuid4(), request())
        assert (await store.budget(principal))["used"] == 0
        assert (await store.budget(principal))["inflight"] == 0
        async with database.sessions() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(Call).where(Call.principal_id == principal)
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.principal_id == principal)
                )
                == 0
            )
    finally:
        async with database.sessions.begin() as session:
            await session.execute(text(f"DROP TRIGGER {trigger} ON gateway.audit_events"))
            await session.execute(text(f"DROP FUNCTION gateway.{function}()"))


async def test_disabled_principal_rejection_is_audited_without_admission(database):
    identity = "missing-" + uuid4().hex
    try:
        denied = await BudgetStore(database).admit(identity, uuid4(), request())
        assert denied.error == "PRINCIPAL_DISABLED"
        async with database.sessions() as session:
            event = await session.scalar(
                select(AuditEvent).where(AuditEvent.principal_id == identity)
            )
            assert event is not None and event.reason == "PRINCIPAL_DISABLED"
            assert (
                await session.scalar(
                    select(func.count()).select_from(Call).where(Call.principal_id == identity)
                )
                == 0
            )
    finally:
        from sqlalchemy import delete

        async with database.sessions.begin() as session:
            await session.execute(delete(AuditEvent).where(AuditEvent.principal_id == identity))


class CommitConfirmationLost(ConnectionError):
    pass


class LoseOneCommitConfirmation:
    """A real commit succeeds, but its acknowledgement is withheld from the caller."""

    def __init__(self, real_sessions):
        self.real_sessions = real_sessions
        self.armed = True

    def __call__(self):
        return self.real_sessions()

    def begin(self):
        return LostAcknowledgement(self)


class LostAcknowledgement:
    def __init__(self, owner):
        self.owner = owner
        self.real_context = owner.real_sessions.begin()

    async def __aenter__(self):
        return await self.real_context.__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        result = await self.real_context.__aexit__(exc_type, exc, tb)
        if exc_type is None and self.owner.armed:
            self.owner.armed = False
            raise CommitConfirmationLost("commit acknowledgement unavailable")
        return result


async def test_admission_commit_confirmation_loss_leaves_replayable_reserved_call(
    database, principal
):
    key = uuid4()
    uncertain = BudgetStore(SimpleNamespace(sessions=LoseOneCommitConfirmation(database.sessions)))
    with pytest.raises(CommitConfirmationLost):
        await uncertain.admit(principal, key, request())
    store = BudgetStore(database)
    replay = await store.admit(principal, key, request())
    assert not replay.is_new and replay.call.state == "reserved"
    assert replay.call.slot_held
    assert (await store.budget(principal))["used"] == 1
    assert await store.abort_reserved(replay.call.id)
    assert (await store.budget(principal))["used"] == 1


async def test_dispatch_commit_confirmation_loss_cannot_reclaim_or_dispatch_again(
    database, principal
):
    store = BudgetStore(database)
    admitted = await store.admit(principal, uuid4(), request())
    uncertain = BudgetStore(SimpleNamespace(sessions=LoseOneCommitConfirmation(database.sessions)))
    with pytest.raises(CommitConfirmationLost):
        await uncertain.claim_dispatch(admitted.call.id)
    call = await store.get_call(admitted.call.id, principal)
    assert call.state == "dispatching" and call.slot_held
    assert not await store.claim_dispatch(call.id)
    assert not await store.abort_reserved(call.id)
    assert (await store.budget(principal))["inflight"] == 1


async def test_failed_terminal_releases_slot_without_refunding_daily_usage(database, principal):
    store = BudgetStore(database)
    admitted = await store.admit(principal, uuid4(), request())
    await store.claim_dispatch(admitted.call.id)
    assert await store.finalize(
        admitted.call.id, ToolEvidence(operation_id=admitted.call.id, state="failed", result=None)
    )
    assert (await store.budget(principal))["inflight"] == 0
    assert (await store.budget(principal))["used"] == 1


async def test_distinct_principals_share_neither_idempotency_nor_quota(database, principal):
    other = "test-" + uuid4().hex
    async with database.sessions.begin() as session:
        session.add(
            Principal(id=other, allowed_tools=["catalog.lookup"], daily_limit=1, max_inflight=1)
        )
    try:
        store = BudgetStore(database)
        key = uuid4()
        first = await store.admit(principal, key, request())
        second = await store.admit(other, key, request())
        assert first.is_new and second.is_new and first.call.id != second.call.id
        assert await store.get_call(first.call.id, other) is None
        assert (await store.budget(other))["used"] == 1
        assert (await store.budget(principal))["used"] == 1
    finally:
        from sqlalchemy import delete

        from toolgate.models import DailyUsage

        async with database.sessions.begin() as session:
            await session.execute(delete(AuditEvent).where(AuditEvent.principal_id == other))
            await session.execute(delete(Call).where(Call.principal_id == other))
            await session.execute(delete(DailyUsage).where(DailyUsage.principal_id == other))
            await session.execute(delete(Principal).where(Principal.id == other))
