"""Connection ownership and guarded PostgreSQL initialization."""

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from toolgate.models import Base


class Database:
    def __init__(self, url: str, *, test_database: bool = False):
        parsed = make_url(url)
        if parsed.drivername != "postgresql+psycopg":
            raise ValueError("PostgreSQL with psycopg is required")
        if test_database and (not parsed.database or not parsed.database.endswith("_test")):
            raise ValueError("Test database name must end in _test")
        self.engine = create_async_engine(
            url, pool_pre_ping=True, isolation_level="READ COMMITTED", hide_parameters=True
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self):
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self):
        await self.engine.dispose()
