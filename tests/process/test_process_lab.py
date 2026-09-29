import asyncio
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest


def load_lab():
    path = Path(__file__).resolve().parents[2] / "scripts" / "process_lab.py"
    assert path.exists(), "real process lab runner has not been implemented"
    spec = importlib.util.spec_from_file_location("process_lab", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("daily,slots,accepted", [(30, 2, 2), (3, 20, 3), (1, 20, 1)])
async def test_two_api_processes_share_admission(daily, slots, accepted):
    async with load_lab().ProcessLab(daily=daily, slots=slots) as lab:
        report = await lab.race(20, accepted)
        assert report["admitted"] == accepted
        assert report["rejected"] == 20 - accepted
        assert report["held_at_barrier"] == accepted
        await lab.restart_api(0)
        assert (await lab.budget())["used"] == accepted


async def test_killed_api_cannot_release_live_tool():
    async with load_lab().ProcessLab(daily=5, slots=1) as lab:
        pending = asyncio.create_task(lab.call(0))
        call = await lab.wait_call()
        await lab.wait_started(1)
        lab.kill_api(0)
        await asyncio.gather(pending, return_exceptions=True)
        assert (await lab.budget())["inflight"] == 1
        rejected = await lab.call(1)
        assert rejected.status_code == 429
        assert await lab.reconcile(call.id) is False
        lab.release()
        await lab.wait_terminal(call.id)
        assert await lab.reconcile(call.id) is True
        assert await lab.reconcile(call.id) is True
        await lab.assert_finished(1)


async def test_terminal_response_loss_reconciles_once_without_resend():
    async with load_lab().ProcessLab(daily=5, slots=1, drop_response=True) as lab:
        pending = asyncio.create_task(lab.call(0))
        call = await lab.wait_call()
        await lab.wait_started(1)
        lab.release()
        response = await pending
        assert response.status_code == 202
        assert (await lab.budget())["inflight"] == 1
        await lab.wait_terminal(call.id)
        outcomes = await asyncio.gather(*[lab.reconcile(call.id) for _ in range(5)])
        assert all(outcomes)
        await lab.assert_finished(1)


async def test_client_tcp_disconnect_does_not_end_live_tool():
    async with load_lab().ProcessLab(daily=5, slots=1) as lab:
        # Closing an actual socket is distinct from cancelling the ASGI handler.
        reader, writer = await asyncio.open_connection("127.0.0.1", lab.api_ports[0])
        writer.write(lab.raw_call())
        await writer.drain()
        call = await lab.wait_call()
        await lab.wait_started(1)
        writer.close()
        await writer.wait_closed()
        assert (await lab.budget())["inflight"] == 1
        assert (await lab.call(1)).status_code == 429
        lab.release()
        await lab.wait_terminal(call.id)
        # Uvicorn may keep the handler alive after disconnect; either path is safe.
        await lab.reconcile(call.id)
        await lab.wait_released()
        await lab.assert_finished(1)


async def test_same_key_across_processes_starts_one_tool():
    async with load_lab().ProcessLab(daily=5, slots=2) as lab:
        key = uuid4()
        start = asyncio.Event()

        async def submit(index):
            await start.wait()
            return await lab.call(index % 2, key)

        pending = [asyncio.create_task(submit(index)) for index in range(20)]
        start.set()
        try:
            await lab.wait_started(1)
            budget = await lab.budget()
            assert budget["used"] == 1 and budget["inflight"] == 1
            conflict = await lab.client.post(
                lab.api_url() + "/v1/calls",
                headers=lab.headers(key),
                json={"tool": "catalog.lookup", "arguments": {"sku": "different"}},
            )
            assert conflict.status_code == 409
            lab.release()
            responses = await asyncio.gather(*pending)
            assert all(response.status_code in {200, 202} for response in responses)
            assert len({response.json()["id"] for response in responses}) == 1
            await lab.assert_finished(1)
        finally:
            lab.release()
            await asyncio.gather(*pending, return_exceptions=True)


def test_lab_entry_requires_explicit_opt_in(monkeypatch):
    from mock_tools import lab_server

    monkeypatch.delenv("TOOLGATE_LAB_MODE", raising=False)
    with pytest.raises(RuntimeError, match="TOOLGATE_LAB_MODE"):
        lab_server.create_app_from_env()


def test_lab_entry_refuses_nontemporary_control_directory(monkeypatch):
    from mock_tools import lab_server

    monkeypatch.setenv("TOOLGATE_LAB_MODE", "1")
    monkeypatch.setenv("TOOLGATE_LAB_DIRECTORY", str(Path.cwd()))
    with pytest.raises(RuntimeError, match="temporary"):
        lab_server.create_app_from_env()
