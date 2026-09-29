"""PostgreSQL arbitration. No process-local lock can authorize an operation."""

from uuid import UUID

from sqlalchemy import func, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mock_tools.models import MockBase, Operation

IMMUTABILITY_DDL = """
CREATE OR REPLACE FUNCTION tools.protect_operation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'operation evidence cannot be deleted';
    END IF;
    IF OLD.state IN ('succeeded', 'failed') THEN
        RAISE EXCEPTION 'terminal operation is immutable';
    END IF;
    IF NEW.operation_id IS DISTINCT FROM OLD.operation_id OR
       NEW.fingerprint IS DISTINCT FROM OLD.fingerprint OR
       NEW.tool IS DISTINCT FROM OLD.tool OR
       NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION 'operation identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;
"""


class OperationStore:
    def __init__(self, database_url: str):
        if not database_url.startswith("postgresql+psycopg://"):
            raise ValueError("Mock tools require PostgreSQL with the psycopg async driver")
        self.database_url = database_url
        self.engine = create_async_engine(database_url, pool_pre_ping=True, hide_parameters=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self):
        async with self.engine.begin() as connection:
            await connection.run_sync(MockBase.metadata.create_all)
            await connection.execute(text(IMMUTABILITY_DDL))
            await connection.execute(
                text("DROP TRIGGER IF EXISTS protect_operation ON tools.operations")
            )
            await connection.execute(
                text(
                    "CREATE TRIGGER protect_operation BEFORE UPDATE OR DELETE ON tools.operations "
                    "FOR EACH ROW EXECUTE FUNCTION tools.protect_operation()"
                )
            )

    async def close(self):
        await self.engine.dispose()

    async def start(self, operation_id: UUID, fingerprint: str, tool: str) -> bool:
        async with self.sessions() as session:
            result = await session.execute(
                insert(Operation)
                .values(
                    operation_id=operation_id, fingerprint=fingerprint, tool=tool, state="running"
                )
                .on_conflict_do_nothing(index_elements=[Operation.operation_id])
                .returning(Operation.operation_id)
            )
            owns_execution = result.scalar_one_or_none() is not None
            # A failed or uncertain commit propagates; callers must not run the tool.
            await session.commit()
            return owns_execution

    async def get(self, operation_id: UUID) -> Operation | None:
        async with self.sessions() as session:
            return await session.get(Operation, operation_id)

    async def finalize(self, operation_id: UUID, state: str, result: dict | None) -> bool:
        if state not in {"succeeded", "failed"}:
            raise ValueError("Expected terminal state")
        async with self.sessions() as session:
            changed = await session.execute(
                update(Operation)
                .where(Operation.operation_id == operation_id, Operation.state == "running")
                .values(state=state, result=result, completed_at=func.now())
                .returning(Operation.operation_id)
            )
            finalized = changed.scalar_one_or_none() is not None
            await session.commit()
            return finalized
