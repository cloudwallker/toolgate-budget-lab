"""Local operation commands. Existing budgets and call history are never reset."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert


async def initialize_tools():
    from mock_tools.store import OperationStore

    store = OperationStore(os.environ["TOOLS_DATABASE_URL"])
    try:
        await store.initialize()
    finally:
        await store.close()


async def seed(daily_limit, max_inflight):
    from toolgate.db import Database
    from toolgate.models import Principal

    database = Database(os.environ["DATABASE_URL"])
    try:
        async with database.sessions() as session:
            for principal, tools in (
                ("alpha", ["catalog.lookup", "catalog.summary"]),
                ("beta", ["catalog.lookup"]),
            ):
                await session.execute(
                    insert(Principal)
                    .values(
                        id=principal,
                        allowed_tools=tools,
                        daily_limit=daily_limit,
                        max_inflight=max_inflight,
                    )
                    .on_conflict_do_nothing(index_elements=[Principal.id])
                )
            await session.commit()
    finally:
        await database.close()


async def reconcile(args):
    import httpx
    from sqlalchemy import select

    from toolgate.budget import BudgetStore
    from toolgate.config import Settings
    from toolgate.db import Database
    from toolgate.models import Call
    from toolgate.reconcile import reap_reserved
    from toolgate.reconcile import reconcile as reconcile_one

    settings = Settings.from_env()
    database = Database(settings.database_url)
    store = BudgetStore(database, timeout_seconds=settings.total_timeout_seconds)
    try:
        if args.reserved:
            async with database.sessions() as session:
                ids = list(
                    (
                        await session.execute(
                            select(Call.id).where(Call.state == "reserved").limit(args.limit)
                        )
                    ).scalars()
                )
            released = sum([await reap_reserved(store, call_id) for call_id in ids])
            return {"checked": len(ids), "released": released}
        async with httpx.AsyncClient(follow_redirects=False) as client:
            call = await reconcile_one(
                store,
                client,
                settings.tool_base_url,
                settings.tool_internal_key,
                args.call_id,
                total_timeout_seconds=settings.total_timeout_seconds,
            )
        return {
            "call_id": str(args.call_id),
            "state": call.state if call else None,
            "slot_held": call.slot_held if call else None,
        }
    finally:
        await database.close()


def main(argv=None):
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    parser = argparse.ArgumentParser(description="ToolGate Budget Lab local operations")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("init-tools")
    seed_parser = commands.add_parser("seed")
    seed_parser.add_argument("--daily-limit", type=int, default=3)
    seed_parser.add_argument("--max-inflight", type=int, default=2)
    reconcile_parser = commands.add_parser("reconcile")
    reconcile_target = reconcile_parser.add_mutually_exclusive_group(required=True)
    reconcile_target.add_argument("--call-id", type=UUID)
    reconcile_target.add_argument("--reserved", action="store_true")
    reconcile_parser.add_argument("--limit", type=int, default=100)
    for name in ("serve-api", "serve-tools"):
        serve_parser = commands.add_parser(name)
        serve_parser.add_argument("--host", default="127.0.0.1")
        serve_parser.add_argument(
            "--port", type=int, default=58083 if name == "serve-tools" else 58081
        )
    args = parser.parse_args(argv)
    if args.command == "migrate":
        from alembic import command
        from alembic.config import Config

        root = Path(__file__).resolve().parents[2]
        command.upgrade(Config(str(root / "alembic.ini")), "head")
    elif args.command == "init-tools":
        asyncio.run(initialize_tools())
    elif args.command == "seed":
        if args.daily_limit < 0 or args.max_inflight < 1:
            parser.error("daily limit must be nonnegative and max inflight positive")
        asyncio.run(seed(args.daily_limit, args.max_inflight))
    elif args.command == "reconcile":
        if not 1 <= args.limit <= 1000:
            parser.error("limit must be between 1 and 1000")
        result = asyncio.run(reconcile(args))
        print(json.dumps(result, default=str))
    else:
        import uvicorn

        factory = (
            "mock_tools.api:create_app_from_env"
            if args.command == "serve-tools"
            else "toolgate.api:create_app_from_env"
        )
        config = uvicorn.Config(
            factory, factory=True, host=args.host, port=args.port, loop="asyncio", access_log=False
        )
        # Uvicorn's run() chooses its own Runner loop factory on Windows.
        # asyncio.run respects the Selector policy required by psycopg.
        asyncio.run(uvicorn.Server(config).serve())


if __name__ == "__main__":
    main()
