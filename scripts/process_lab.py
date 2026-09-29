"""Real subprocess experiments. Credentials remain in environment, never reports."""

import argparse
import asyncio
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx
import uvicorn
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url

from mock_tools.store import OperationStore
from toolgate.budget import BudgetStore
from toolgate.config import Settings
from toolgate.db import Database
from toolgate.models import AuditEvent, Call, DailyUsage, Principal

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = str(Path(__file__).resolve())


def summarize_monotonic(journal, operation_ids, limit):
    """Reject missing evidence and compute peak from one tools process's clock."""
    assert {row["operation_id"] for row in journal} == set(operation_ids)
    assert len(journal) == 2 * len(operation_ids)
    assert all(type(row["seq"]) is int for row in journal)
    assert [row["seq"] for row in journal] == list(range(1, len(journal) + 1))
    active_ids = set()
    completed = set()
    previous_time = -1
    peak = 0
    for row in journal:
        identity = row["operation_id"]
        assert isinstance(row["time_ns"], int) and row["time_ns"] >= previous_time
        previous_time = row["time_ns"]
        if row["event"] == "started":
            assert identity not in active_ids and identity not in completed
            active_ids.add(identity)
        else:
            assert row["event"] == "terminal" and identity in active_ids
            active_ids.remove(identity)
            completed.add(identity)
        peak = max(peak, len(active_ids))
    assert not active_ids and completed == set(operation_ids)
    assert peak <= limit
    return peak


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ProcessLab:
    def __init__(self, *, daily=30, slots=2, drop_response=False, delay_seconds=0):
        self.daily, self.slots, self.drop_response = daily, slots, drop_response
        self.delay_seconds = delay_seconds
        self.principal = "lab-" + uuid4().hex
        self.key, self.internal_key = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.api_ports, self.tools_port = [free_port(), free_port()], free_port()
        self.processes, self.logs = {}, []
        self.temporary = tempfile.TemporaryDirectory(prefix="toolgate-lab-")
        self.directory = Path(self.temporary.name)
        self.client = httpx.AsyncClient(timeout=30, trust_env=False)
        self.database = self.tools = None
        self.wall_clock_regressions = []

    async def __aenter__(self):
        try:
            gateway_url = os.environ.get("TOOLGATE_TEST_DATABASE_URL")
            tools_url = os.environ.get("MOCK_TOOLS_TEST_DATABASE_URL")
            if not gateway_url or not tools_url:
                raise RuntimeError("Both test database URLs are required; run scripts/test.ps1")
            tools_database = make_url(tools_url).database
            if not tools_database or not tools_database.endswith("_test"):
                raise ValueError("Tools test database name must end in _test")
            self.database = Database(gateway_url, test_database=True)
            self.tools = OperationStore(tools_url)
            await self.database.initialize()
            await self.tools.initialize()
            async with self.database.sessions.begin() as session:
                session.add(
                    Principal(
                        id=self.principal,
                        allowed_tools=["catalog.lookup"],
                        daily_limit=self.daily,
                        max_inflight=self.slots,
                    )
                )
            self.environment = dict(
                os.environ,
                DATABASE_URL=gateway_url,
                TOOLS_DATABASE_URL=tools_url,
                TOOLS_INTERNAL_KEY=self.internal_key,
                TOOL_INTERNAL_KEY=self.internal_key,
                TOOL_BASE_URL=f"http://127.0.0.1:{self.tools_port}",
                PRINCIPAL_KEYS=json.dumps({self.key: self.principal}),
                TOTAL_TIMEOUT_SECONDS="20",
                TOOLGATE_LAB_MODE="1",
                TOOLGATE_LAB_DIRECTORY=str(self.directory),
                PYTHONUTF8="1",
                TOOLGATE_LAB_DELAY_SECONDS=str(self.delay_seconds),
                PYTHONPATH=str(ROOT / "src"),
            )
            if self.drop_response:
                (self.directory / "drop-response").touch()
            self.spawn("tools", ["-m", "mock_tools.lab_server", "--port", str(self.tools_port)])
            await self.ready(
                f"http://127.0.0.1:{self.tools_port}/operations/{uuid4()}",
                {"Authorization": f"Bearer {self.internal_key}"},
                status=404,
            )
            for index in range(2):
                await self.restart_api(index)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    def spawn(self, name, arguments):
        log = open(self.directory / f"{name}-{uuid4().hex}.log", "wb")
        self.logs.append(log)
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.processes[name] = subprocess.Popen(
            [sys.executable, *arguments],
            cwd=ROOT,
            env=self.environment,
            stdout=log,
            stderr=log,
            creationflags=flags,
        )

    async def ready(self, url, headers=None, status=200):
        observed = None
        for _ in range(150):
            try:
                response = await self.client.get(url, headers=headers)
                observed = response.status_code
                if response.status_code == status:
                    return
            except httpx.HTTPError:
                pass
            exited = [name for name, proc in self.processes.items() if proc.poll() is not None]
            if exited:
                raise AssertionError("owned service exited before ready: " + ",".join(exited))
            await asyncio.sleep(0.05)
        diagnostics = []
        for log in self.logs:
            log.flush()
            content = await asyncio.to_thread(
                Path(log.name).read_text, encoding="utf-8", errors="replace"
            )
            lines = content.splitlines()
            diagnostics.extend(
                line.split(":", 1)[0]
                for line in lines
                if "Error:" in line and not line.startswith(" ")
            )
        raise AssertionError(f"owned service readiness HTTP {observed}: {sorted(set(diagnostics))}")

    def kill_api(self, index):
        process = self.processes.get(f"api-{index}")
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    async def restart_api(self, index):
        self.kill_api(index)
        self.spawn(f"api-{index}", [SCRIPT, "serve-api", "--port", str(self.api_ports[index])])
        await self.ready(self.api_url(index) + "/health/ready")

    def api_url(self, index=1):
        return f"http://127.0.0.1:{self.api_ports[index]}"

    def headers(self, key=None):
        return {"Authorization": f"Bearer {self.key}", "Idempotency-Key": str(key or uuid4())}

    async def call(self, index, key=None):
        return await self.client.post(
            self.api_url(index) + "/v1/calls",
            headers=self.headers(key),
            json={"tool": "catalog.lookup", "arguments": {"sku": "SKU-001"}},
        )

    def raw_call(self):
        body = json.dumps({"tool": "catalog.lookup", "arguments": {"sku": "SKU-001"}}).encode()
        head = (
            f"POST /v1/calls HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Authorization: Bearer {self.key}\r\nIdempotency-Key: {uuid4()}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
        )
        return head.encode() + body

    async def budget(self):
        response = await self.client.get(self.api_url() + "/v1/budget", headers=self.headers())
        assert response.status_code == 200
        return response.json()

    async def calls(self):
        async with self.database.sessions() as session:
            return list(
                (
                    await session.scalars(select(Call).where(Call.principal_id == self.principal))
                ).all()
            )

    async def wait_call(self):
        for _ in range(150):
            calls = await self.calls()
            if calls:
                return calls[0]
            await asyncio.sleep(0.02)
        raise AssertionError("call was never admitted")

    async def wait_started(self, count):
        for _ in range(300):
            if len(list(self.directory.glob("started-*"))) == count:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("expected number of tools did not reach barrier")

    def release(self):
        (self.directory / "release").touch()

    async def wait_terminal(self, operation_id):
        for _ in range(150):
            operation = await self.tools.get(operation_id)
            if operation and operation.state in {"succeeded", "failed"}:
                return operation
            await asyncio.sleep(0.02)
        raise AssertionError("tool did not reach durable terminal state")

    async def reconcile(self, call_id):
        from toolgate.reconcile import reconcile

        settings = Settings.from_env(self.environment)
        call = await reconcile(
            BudgetStore(self.database),
            self.client,
            settings.tool_base_url,
            settings.tool_internal_key,
            call_id,
            total_timeout_seconds=20,
        )
        return call is not None and not call.slot_held

    async def wait_released(self):
        for _ in range(150):
            if (await self.budget())["inflight"] == 0:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("terminal call was not released")

    async def assert_finished(self, count):
        budget = await self.budget()
        assert budget["used"] == count and budget["inflight"] == 0
        calls = await self.calls()
        assert len(calls) == count
        assert len(list(self.directory.glob("started-*"))) == count
        self.wall_clock_regressions = []
        for call in calls:
            operation = await self.tools.get(call.id)
            assert operation is not None and operation.state == "succeeded"
            assert operation.completed_at is not None
            if operation.completed_at < operation.started_at:
                self.wall_clock_regressions.append(str(call.id))
        summarize_monotonic(
            await self.monotonic_journal(), {str(call.id) for call in calls}, self.slots
        )
        async with self.database.sessions() as session:
            released = await session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.principal_id == self.principal, AuditEvent.event == "RELEASED")
            )
            assert released == count

    async def monotonic_journal(self):
        path = self.directory / "lifecycle.jsonl"
        if not path.exists():
            return []
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
        return [json.loads(line) for line in content.splitlines() if line]

    async def race(self, requests, accepted):
        start = asyncio.Event()

        async def contender(index):
            await start.wait()
            begin = perf_counter()
            response = await self.call(index % 2)
            return response.status_code, (perf_counter() - begin) * 1000

        tasks = [asyncio.create_task(contender(index)) for index in range(requests)]
        start.set()
        try:
            await self.wait_started(accepted)
            for _ in range(200):
                if sum(task.done() for task in tasks) == requests - accepted:
                    break
                await asyncio.sleep(0.02)
            assert sum(task.done() for task in tasks) == requests - accepted
            budget = await self.budget()
            assert budget["inflight"] == accepted and budget["used"] == accepted
            assert len(await self.calls()) == accepted
            self.release()
            results = await asyncio.gather(*tasks)
            assert sum(200 <= status < 300 for status, _ in results) == accepted
            assert sum(status == 429 for status, _ in results) == requests - accepted
            await self.assert_finished(accepted)
            return {
                "requests": requests,
                "admitted": accepted,
                "rejected": requests - accepted,
                "held_at_barrier": budget["inflight"],
                "latency_ms": [round(latency, 2) for _, latency in results],
            }
        finally:
            self.release()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def __aexit__(self, *exception):
        self.release()
        for process in self.processes.values():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        await self.client.aclose()
        if self.database:
            async with self.database.sessions.begin() as session:
                for model in [AuditEvent, Call, DailyUsage, Principal]:
                    field = model.id if model is Principal else model.principal_id
                    await session.execute(delete(model).where(field == self.principal))
            await self.database.close()
        if self.tools:
            await self.tools.close()
        for log in self.logs:
            log.close()
        self.temporary.cleanup()


async def experiment():
    reports = []
    for daily, slots, admitted in [(30, 2, 2), (3, 20, 3), (1, 20, 1)]:
        async with ProcessLab(daily=daily, slots=slots) as lab:
            reports.append(await lab.race(20, admitted))
    print(json.dumps({"mode": "real-loopback-processes", "reports": reports}, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["serve-api", "run"])
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if args.command == "serve-api":
        from toolgate.api import create_app

        config = uvicorn.Config(
            create_app(Settings.from_env()),
            host="127.0.0.1",
            port=args.port,
            loop="asyncio",
            access_log=False,
            log_level="critical",
        )
        asyncio.run(uvicorn.Server(config).serve())
    else:
        asyncio.run(experiment())


if __name__ == "__main__":
    main()
