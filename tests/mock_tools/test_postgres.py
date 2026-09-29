import asyncio
import os
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update


@pytest_asyncio.fixture
async def app():
    from mock_tools.api import create_app

    url = os.environ.get("MOCK_TOOLS_TEST_DATABASE_URL")
    if not url:
        raise RuntimeError("MOCK_TOOLS_TEST_DATABASE_URL is required for real PostgreSQL tests")
    from sqlalchemy.engine import make_url

    if not (make_url(url).database or "").endswith("_test"):
        raise RuntimeError("Mock tools tests require an isolated database ending in _test")
    application = create_app(url, "test-internal-key")
    await application.state.store.initialize()
    yield application
    await application.state.store.close()


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://tools",
        headers={"Authorization": "Bearer test-internal-key"},
    )


async def test_terminal_persistence_failure_is_sanitized_and_keeps_running(
    app, monkeypatch, caplog
):
    from sqlalchemy.exc import DBAPIError

    marker = "synthetic-private-result-marker"

    async def unavailable(*args):
        raise DBAPIError("UPDATE tools.operations", {"result": marker}, Exception(marker))

    monkeypatch.setattr(app.state.store, "finalize", unavailable)
    operation_id = uuid4()
    async with client_for(app) as client:
        response = await client.post(
            f"/operations/{operation_id}",
            json={"tool": "catalog.lookup", "arguments": {"sku": "SKU-001"}},
        )
    assert response.status_code == 503
    assert marker not in response.text and marker not in caplog.text
    assert (await app.state.store.get(operation_id)).state == "running"


@pytest.mark.asyncio
async def test_started_is_committed_before_business_and_concurrent_duplicate_never_executes(app):
    from mock_tools.api import create_app
    from mock_tools.models import Operation

    entered, release = asyncio.Event(), asyncio.Event()
    starts = []

    async def hook(operation_id, request):
        async with app.state.store.sessions() as session:
            row = await session.get(Operation, operation_id)
            assert row.state == "running"
            assert row.result is None
        starts.append(operation_id)
        entered.set()
        await release.wait()

    # A separate engine/application exercises database arbitration, not a process-local lock.
    other = create_app(app.state.store.database_url, "test-internal-key", hook=hook)
    app.state.hook = hook
    operation_id = uuid4()
    path = f"/operations/{operation_id}"
    payload = {"tool": "catalog.lookup", "arguments": {"sku": "SKU-001"}}
    try:
        async with client_for(app) as first, client_for(other) as second:
            pending = asyncio.create_task(first.post(path, json=payload))
            await asyncio.wait_for(entered.wait(), 5)
            running = await second.get(path)
            assert running.json() == {
                "operation_id": str(operation_id),
                "state": "running",
                "result": None,
            }
            duplicate = await second.post(path, json=payload)
            assert duplicate.status_code == 409
            release.set()
            terminal = await pending
            assert terminal.status_code == 200
            assert terminal.json()["state"] == "succeeded"
            replay = await second.post(path, json=payload)
            assert replay.json() == terminal.json()
            assert starts == [operation_id]
            conflict = await second.post(
                path, json={"tool": "catalog.summary", "arguments": {"sku": "SKU-001"}}
            )
            assert conflict.status_code == 409
    finally:
        release.set()
        await other.state.store.close()


@pytest.mark.asyncio
async def test_failure_persists_terminal_and_later_post_does_not_restart(app):
    from mock_tools.api import create_app

    async def fail(operation_id, request):
        raise RuntimeError("simulated business failure")

    app.state.hook = fail
    path = f"/operations/{uuid4()}"
    payload = {"tool": "catalog.lookup", "arguments": {"sku": "SKU-001"}}
    async with client_for(app) as client:
        response = await client.post(path, json=payload)
        assert response.status_code == 200
        assert response.json()["state"] == "failed"
        assert response.json()["result"] is None
    other = create_app(app.state.store.database_url, "test-internal-key")
    try:
        async with client_for(other) as client:
            assert (await client.get(path)).json() == response.json()
            assert (await client.post(path, json=payload)).json() == response.json()
    finally:
        await other.state.store.close()


@pytest.mark.asyncio
async def test_cancelled_execution_keeps_running_without_restart(app):
    entered = asyncio.Event()

    async def blocked(operation_id, request):
        entered.set()
        await asyncio.Event().wait()

    app.state.hook = blocked
    path = f"/operations/{uuid4()}"
    payload = {"tool": "catalog.summary", "arguments": {"sku": "SKU-001"}}
    async with client_for(app) as client:
        pending = asyncio.create_task(client.post(path, json=payload))
        await asyncio.wait_for(entered.wait(), 5)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert (await client.get(path)).json()["state"] == "running"
        assert (await client.post(path, json=payload)).status_code == 409


@pytest.mark.asyncio
async def test_terminal_immutable_in_database_and_finalize_compare_and_set(app):
    from sqlalchemy.exc import DBAPIError

    from mock_tools.models import Operation

    operation_id = uuid4()
    payload = {"tool": "catalog.lookup", "arguments": {"sku": "SKU-002"}}
    async with client_for(app) as client:
        terminal = (await client.post(f"/operations/{operation_id}", json=payload)).json()
        assert (await client.get(f"/operations/{uuid4()}")).status_code == 404
    assert not await app.state.store.finalize(operation_id, "failed", None)
    async with app.state.store.sessions() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                update(Operation)
                .where(Operation.operation_id == operation_id)
                .values(state="failed", result=None)
            )
            await session.commit()
        await session.rollback()
        row = (
            await session.execute(select(Operation).where(Operation.operation_id == operation_id))
        ).scalar_one()
        assert row.state == terminal["state"]
        assert row.result == terminal["result"]
