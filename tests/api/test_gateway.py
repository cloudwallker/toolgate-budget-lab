"""API with real admission transactions; HTTP fault transport controls external boundaries."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from toolgate.budget import BudgetStore
from toolgate.config import Settings
from toolgate.contracts import CallRequest


def settings(principal, timeout="1"):
    return Settings.from_env(
        {
            "DATABASE_URL": "postgresql+psycopg://unused/toolgate_test",
            "TOOL_BASE_URL": "http://tools",
            "TOOL_INTERNAL_KEY": "test-internal",
            "PRINCIPAL_KEYS": '{"test-api":"' + principal + '"}',
            "TOTAL_TIMEOUT_SECONDS": timeout,
        }
    )


def payload(sku="A"):
    return {"tool": "catalog.lookup", "arguments": {"sku": sku}}


def headers(key=None):
    return {"Authorization": "Bearer test-api", "Idempotency-Key": str(key or uuid4())}


async def test_success_replay_and_query_isolation(database, principal):
    from toolgate.api import create_app

    sent = []

    def upstream(request):
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "operation_id": request.url.path.split("/")[-1],
                "state": "succeeded",
                "result": {"price": 100},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as tools:
        app = create_app(settings(principal), database, tools)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://api"
        ) as api:
            key = uuid4()
            first = await api.post("/v1/calls", headers=headers(key), json=payload())
            assert first.status_code == 200
            assert first.headers["server-timing"].startswith("admission;dur=")
            replay = await api.post("/v1/calls", headers=headers(key), json=payload())
            assert replay.json() == first.json()
            conflict = await api.post("/v1/calls", headers=headers(key), json=payload("B"))
            assert conflict.status_code == 409
            assert len(sent) == 1
            assert sent[0].headers["authorization"] == "Bearer test-internal"
            assert first.json()["state"] == "succeeded" and not first.json()["slot_held"]
            call_id = first.json()["id"]
            app.state.settings = settings("other")
            hidden = await api.get(f"/v1/calls/{call_id}", headers=headers())
            assert hidden.status_code == 404
            app.state.settings = settings(principal)
            budget = await api.get("/v1/budget", headers=headers())
            assert budget.json()["used"] == 1 and budget.json()["inflight"] == 0


async def test_auth_validation_and_tool_denial_never_send_or_leak(database, principal):
    from toolgate.api import create_app

    def forbidden_send(request):
        raise AssertionError("Rejected request reached tool service")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden_send)) as tools:
        app = create_app(settings(principal), database, tools)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://api"
        ) as api:
            response = await api.post(
                "/v1/calls", headers={"Authorization": "Bearer private-secret"}, json=payload()
            )
            assert response.status_code == 401 and "private-secret" not in response.text
            invalid = await api.post(
                "/v1/calls", headers=headers(), json={**payload(), "actor": "private-input"}
            )
            assert invalid.status_code == 422 and "private-input" not in invalid.text
            denied = await api.post(
                "/v1/calls", headers=headers(), json={**payload(), "tool": "catalog.summary"}
            )
            assert denied.status_code == 403
            assert (await app.state.store.budget(principal))["used"] == 0


@pytest.mark.parametrize("fault", ["wrong_id", "running", "invalid", "500", "redirect"])
async def test_untrusted_response_preserves_slot(database, principal, fault):
    from toolgate.api import create_app

    sent = []

    def upstream(request):
        sent.append(request)
        body = {
            "operation_id": str(uuid4())
            if fault == "wrong_id"
            else request.url.path.split("/")[-1],
            "state": "running" if fault == "running" else "succeeded",
            "result": None if fault == "running" else {"ok": True},
        }
        if fault == "invalid":
            body = {"private": "private-response"}
        return httpx.Response(
            500 if fault == "500" else 302 if fault == "redirect" else 200,
            json=body,
            headers={"Location": "http://other"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(upstream), follow_redirects=True
    ) as tools:
        app = create_app(settings(principal), database, tools)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://api"
        ) as api:
            response = await api.post("/v1/calls", headers=headers(), json=payload())
            assert response.status_code == 202
            assert response.json()["slot_held"] and response.json()["state"] == "unknown"
            assert len(sent) == 1 and "private-response" not in response.text


async def test_total_deadline_and_cancel_keep_unknown_held(database, principal):
    from toolgate.execution import execute_call

    store = BudgetStore(database)
    request = CallRequest(**payload())
    reached = asyncio.Event()

    async def hangs(http_request):
        reached.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(hangs)) as client:
        one = (await store.admit(principal, uuid4(), request)).call
        await execute_call(store, client, "http://tools", "test-internal", one.id, request, 0.05)
        assert (await store.get_call(one.id)).state == "unknown"
        reached.clear()
        two = (await store.admit(principal, uuid4(), request)).call
        task = asyncio.create_task(
            execute_call(store, client, "http://tools", "test-internal", two.id, request, 5)
        )
        await reached.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await store.get_call(two.id)).state == "unknown"
        assert (await store.budget(principal))["inflight"] == 2


async def test_dispatch_commit_confirmation_loss_never_sends(database, principal):
    from toolgate.execution import execute_call

    real = BudgetStore(database)
    request = CallRequest(**payload())
    call = (await real.admit(principal, uuid4(), request)).call

    class LostCommit:
        async def claim_dispatch(self, call_id):
            await real.claim_dispatch(call_id)
            raise ConnectionError("private-database-string")

        async def get_call(self, call_id):
            return await real.get_call(call_id)

    def no_send(request):
        raise AssertionError("Unconfirmed dispatch must not send")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_send)) as client:
        with pytest.raises(ConnectionError):
            await execute_call(
                LostCommit(), client, "http://tools", "test-internal", call.id, request
            )
    assert (await real.get_call(call.id)).slot_held


async def test_admission_commit_confirmation_loss_returns_safe_503(database, principal):
    from toolgate.api import create_app

    real = BudgetStore(database)

    class LostAdmission:
        async def admit(self, principal_id, key, request):
            await real.admit(principal_id, key, request)
            raise ConnectionError("private-db-password")

    def no_send(request):
        raise AssertionError("Unconfirmed admission must not send")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_send)) as tools:
        app = create_app(settings(principal), database, tools)
        app.state.store = LostAdmission()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://api"
        ) as api:
            key = uuid4()
            response = await api.post("/v1/calls", headers=headers(key), json=payload())
            assert response.status_code == 503 and "private-db-password" not in response.text
            replay = await real.admit(principal, key, CallRequest(**payload()))
            assert not replay.is_new and replay.call.state == "reserved"
            assert (await real.budget(principal))["used"] == 1


async def test_invalid_request_with_audit_outage_is_sanitized_503(database, principal):
    from toolgate.api import create_app

    class AuditOutage:
        async def audit_rejection(self, principal_id, reason):
            raise ConnectionError("private-audit-credential")

    app = create_app(settings(principal), database)
    app.state.store = AuditOutage()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app, raise_app_exceptions=False), base_url="http://api"
    ) as api:
        response = await api.post(
            "/v1/calls", headers=headers(), json={**payload(), "actor": "private-input"}
        )
        assert response.status_code == 503
        assert "private" not in response.text


async def test_disabled_authenticated_principal_budget_is_forbidden(database, principal):
    from toolgate.api import create_app

    app = create_app(settings("not-in-database"), database)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://api") as api:
        response = await api.get("/v1/budget", headers=headers())
        assert response.status_code == 403


@pytest.mark.parametrize("state", ["missing", "running", "succeeded"])
async def test_reconcile_only_terminal_and_reserved_reap(database, principal, state):
    from toolgate.reconcile import reap_reserved, reconcile

    store = BudgetStore(database)
    call = (await store.admit(principal, uuid4(), CallRequest(**payload()))).call
    assert not await reap_reserved(store, call.id, now=datetime.now(UTC))
    assert await store.claim_dispatch(call.id)
    assert not await reap_reserved(store, call.id, now=call.deadline + timedelta(seconds=1))
    sent = []

    def upstream(request):
        sent.append(request)
        return httpx.Response(
            404 if state == "missing" else 200,
            json={
                "operation_id": str(call.id),
                "state": "running" if state == "running" else "succeeded",
                "result": None if state == "running" else {"ok": True},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        current = await reconcile(store, client, "http://tools", "test-internal", call.id)
        assert current.slot_held == (state != "succeeded")
        await reconcile(store, client, "http://tools", "test-internal", call.id)
        assert all(r.method == "GET" for r in sent)
    other = (await store.admit(principal, uuid4(), CallRequest(**payload()))).call
    assert await reap_reserved(store, other.id, now=other.deadline + timedelta(seconds=1))
    assert (await store.get_call(other.id)).state == "aborted"
