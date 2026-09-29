"""Explicit loopback-only lab runner; never part of the production tools entry."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from time import monotonic_ns
from uuid import UUID

import uvicorn

from mock_tools.api import create_app


class LoseTerminalResponse:
    def __init__(self, app, directory, journal):
        self.app = app
        self.directory = directory
        self.journal = journal

    async def __call__(self, scope, receive, send):
        drop = (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and (self.directory / "drop-response").exists()
        )

        async def record_terminal(message):
            if (
                message["type"] == "http.response.start"
                and message["status"] == 200
                and scope.get("path", "").startswith("/operations/")
            ):
                try:
                    operation_id = UUID(scope["path"].removeprefix("/operations/"))
                except ValueError:
                    operation_id = None
                if operation_id is not None and str(operation_id) in self.journal.started:
                    operation = await self.app.state.store.get(operation_id)
                    if operation and operation.state in {"succeeded", "failed"}:
                        self.journal.record(operation_id, "terminal")

        async def observe(message):
            await record_terminal(message)
            await send(message)

        if not drop:
            return await self.app(scope, receive, observe)
        messages = []

        async def capture(message):
            # Observe before capturing: a concurrent GET must not expose terminal
            # evidence before its monotonic lifecycle event is persisted.
            await record_terminal(message)
            messages.append(message)

        await self.app(scope, receive, capture)
        # The operation is durably terminal, but the real TCP response is truncated.
        await send(
            {"type": "http.response.start", "status": 200, "headers": [(b"content-length", b"1")]}
        )
        await send({"type": "http.response.body", "body": b"", "more_body": False})


class MonotonicJournal:
    """One tools event loop serializes these synchronous, no-await writes."""

    def __init__(self, directory):
        self.path = directory / "lifecycle.jsonl"
        self.started = set()
        self.recorded = set()
        self.sequence = 0

    def record(self, operation_id, event):
        identity = str(operation_id)
        pair = (identity, event)
        if pair in self.recorded:
            return
        self.sequence += 1
        row = {
            "operation_id": identity,
            "event": event,
            "time_ns": monotonic_ns(),
            "seq": self.sequence,
        }
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self.recorded.add(pair)
        if event == "started":
            self.started.add(identity)


def create_app_from_env():
    if os.environ.get("TOOLGATE_LAB_MODE") != "1":
        raise RuntimeError("TOOLGATE_LAB_MODE=1 is required")
    directory = Path(os.environ["TOOLGATE_LAB_DIRECTORY"]).resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if not directory.is_relative_to(temporary_root) or directory == temporary_root:
        raise RuntimeError("lab control directory must be a dedicated temporary directory")
    if not directory.is_dir() or not directory.name.startswith("toolgate-lab-"):
        raise RuntimeError("lab control directory must be a dedicated temporary directory")

    journal = MonotonicJournal(directory)

    async def barrier(operation_id, request):
        journal.record(operation_id, "started")
        (directory / f"started-{operation_id}").touch()
        while not (directory / "release").exists():  # noqa: ASYNC110 - cross-process file barrier
            await asyncio.sleep(0.02)
        delay = float(os.environ.get("TOOLGATE_LAB_DELAY_SECONDS", "0"))
        if not 0 <= delay <= 5:
            raise RuntimeError("lab delay must be between zero and five seconds")
        await asyncio.sleep(delay)

    app = create_app(
        os.environ["TOOLS_DATABASE_URL"], os.environ["TOOLS_INTERNAL_KEY"], hook=barrier
    )
    return LoseTerminalResponse(app, directory, journal)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    config = uvicorn.Config(
        create_app_from_env(),
        host="127.0.0.1",
        port=args.port,
        loop="asyncio",
        access_log=False,
        log_level="critical",
    )
    asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
