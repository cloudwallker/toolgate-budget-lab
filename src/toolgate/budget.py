"""Serialize admission and every occupancy transition on the principal row.

Returning from a mutation means the transaction commit was acknowledged.
Exceptions (including ambiguous commits) must never authorize a network send.
"""

from dataclasses import dataclass, replace
from datetime import UTC, timedelta
from time import perf_counter

from sqlalchemy import func, select, text

from toolgate.contracts import fingerprint
from toolgate.models import AuditEvent, Call, DailyUsage, Principal


@dataclass(frozen=True)
class Admission:
    call: Call | None = None
    is_new: bool = False
    error: str | None = None
    decision_ms: float = 0
    lock_ms: float = 0


class BudgetStore:
    def __init__(self, database, *, clock=None, timeout_seconds=10):
        self.database = database
        self.clock = clock
        self.timeout_seconds = timeout_seconds

    async def admit(self, principal_id, request_key, request):
        started = perf_counter()
        result = await self._admit(principal_id, request_key, request)
        return replace(result, decision_ms=(perf_counter() - started) * 1000)

    async def _admit(self, principal_id, request_key, request):
        started = perf_counter()
        digest = fingerprint(request)
        async with self.database.sessions.begin() as session:
            lock_started = perf_counter()
            principal = await self._lock_principal(session, principal_id)
            lock_ms = (perf_counter() - lock_started) * 1000
            if principal is None:
                self._audit(session, principal_id, None, "REJECTED", "PRINCIPAL_DISABLED")
                return Admission(error="PRINCIPAL_DISABLED", lock_ms=lock_ms)
            if request.tool not in principal.allowed_tools:
                self._audit(session, principal_id, None, "REJECTED", "TOOL_DENIED")
                return Admission(error="TOOL_DENIED", lock_ms=lock_ms)
            existing = await session.scalar(
                select(Call).where(
                    Call.principal_id == principal_id, Call.request_key == request_key
                )
            )
            if existing is not None:
                if existing.request_fingerprint != digest:
                    self._audit(
                        session, principal_id, existing.id, "REJECTED", "IDEMPOTENCY_CONFLICT"
                    )
                    return Admission(error="IDEMPOTENCY_CONFLICT", lock_ms=lock_ms)
                self._audit(session, principal_id, existing.id, "REPLAYED")
                return Admission(call=existing, lock_ms=lock_ms)
            now = await self._now(session)
            usage = await session.get(DailyUsage, (principal_id, now.date()))
            used = usage.used if usage else 0
            if used >= principal.daily_limit:
                self._audit(session, principal_id, None, "REJECTED", "DAILY_BUDGET_EXHAUSTED")
                return Admission(error="DAILY_BUDGET_EXHAUSTED", lock_ms=lock_ms)
            held = await self._held(session, principal_id)
            if held >= principal.max_inflight:
                self._audit(session, principal_id, None, "REJECTED", "CONCURRENCY_EXHAUSTED")
                return Admission(error="CONCURRENCY_EXHAUSTED", lock_ms=lock_ms)
            if usage is None:
                usage = DailyUsage(principal_id=principal_id, utc_date=now.date(), used=0)
                session.add(usage)
            usage.used += 1
            decision_ms = (perf_counter() - started) * 1000
            call = Call(
                principal_id=principal_id,
                request_key=request_key,
                request_fingerprint=digest,
                tool=request.tool,
                quota_date=now.date(),
                state="reserved",
                slot_held=True,
                deadline=now + timedelta(seconds=self.timeout_seconds),
                decision_ms=decision_ms,
            )
            session.add(call)
            await session.flush()
            self._audit(session, principal_id, call.id, "ADMITTED")
            admission = Admission(call=call, is_new=True, decision_ms=decision_ms, lock_ms=lock_ms)
        return admission

    async def claim_dispatch(self, call_id):
        async with self.database.sessions.begin() as session:
            call = await self._lock_call(session, call_id)
            if call is None or call.state != "reserved":
                return False
            call.state = "dispatching"
            self._audit(session, call.principal_id, call.id, "DISPATCHED")
        return True

    async def abort_reserved(self, call_id):
        async with self.database.sessions.begin() as session:
            call = await self._lock_call(session, call_id)
            if call is None or call.state != "reserved":
                return False
            call.state, call.slot_held = "aborted", False
            self._audit(session, call.principal_id, call.id, "RELEASED", "NEVER_DISPATCHED")
        return True

    async def mark_unknown(self, call_id, reason):
        async with self.database.sessions.begin() as session:
            call = await self._lock_call(session, call_id)
            if call is None or call.state not in ("dispatching", "running", "unknown"):
                return False
            call.state = "unknown"
            self._audit(session, call.principal_id, call.id, "UNKNOWN", reason)
        return True

    async def finalize(self, call_id, evidence):
        if not evidence.matches(call_id) or not evidence.is_terminal:
            return False
        async with self.database.sessions.begin() as session:
            call = await self._lock_call(session, call_id)
            if call is None or call.state not in ("dispatching", "running", "unknown"):
                return False
            call.state, call.slot_held = evidence.state, False
            call.result = evidence.result
            call.evidence = evidence.model_dump(mode="json")
            self._audit(session, call.principal_id, call.id, "RELEASED", "TERMINAL_EVIDENCE")
        return True

    async def get_call(self, call_id, principal_id=None):
        async with self.database.sessions() as session:
            query = select(Call).where(Call.id == call_id)
            if principal_id is not None:
                query = query.where(Call.principal_id == principal_id)
            return await session.scalar(query)

    async def budget(self, principal_id):
        async with self.database.sessions.begin() as session:
            principal = await self._lock_principal(session, principal_id)
            if principal is None:
                return None
            now = await self._now(session)
            usage = await session.get(DailyUsage, (principal_id, now.date()))
            used = usage.used if usage else 0
            return {
                "principal_id": principal_id,
                "utc_date": now.date().isoformat(),
                "used": used,
                "daily_limit": principal.daily_limit,
                "remaining": principal.daily_limit - used,
                "max_inflight": principal.max_inflight,
                "inflight": await self._held(session, principal_id),
            }

    async def audit_rejection(self, principal_id, reason):
        async with self.database.sessions.begin() as session:
            self._audit(session, principal_id, None, "REJECTED", reason)

    async def recoverable_calls(self, *, reserved=False):
        async with self.database.sessions() as session:
            query = select(Call).where(Call.slot_held.is_(True))
            if reserved:
                query = query.where(
                    Call.state == "reserved", Call.deadline <= func.clock_timestamp()
                )
            else:
                query = query.where(Call.state.in_(("dispatching", "running", "unknown")))
            return list((await session.scalars(query.order_by(Call.created_at))).all())

    async def _lock_principal(self, session, principal_id):
        await session.execute(text("SET LOCAL lock_timeout = '2s'"))
        return await session.scalar(
            select(Principal).where(Principal.id == principal_id).with_for_update()
        )

    async def _lock_call(self, session, call_id):
        # Read only the identity before locking; never acquire a call lock first.
        principal_id = await session.scalar(select(Call.principal_id).where(Call.id == call_id))
        if principal_id is None:
            return None
        await self._lock_principal(session, principal_id)
        return await session.scalar(select(Call).where(Call.id == call_id).with_for_update())

    async def _now(self, session):
        value = self.clock() if self.clock else await session.scalar(select(func.clock_timestamp()))
        return value.astimezone(UTC)

    @staticmethod
    async def _held(session, principal_id):
        return await session.scalar(
            select(func.count())
            .select_from(Call)
            .where(Call.principal_id == principal_id, Call.slot_held.is_(True))
        )

    @staticmethod
    def _audit(session, principal_id, call_id, event, reason=None):
        session.add(
            AuditEvent(principal_id=principal_id, call_id=call_id, event=event, reason=reason)
        )
