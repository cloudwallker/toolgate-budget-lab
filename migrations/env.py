"""Explicit migrations; URLs are obtained from the environment and never logged."""

import os

from alembic import context
from sqlalchemy import create_engine

from toolgate.models import Base

url = os.environ["DATABASE_URL"]
if not url.startswith("postgresql+psycopg://"):
    raise ValueError("DATABASE_URL requires PostgreSQL psycopg")

options = dict(target_metadata=Base.metadata, include_schemas=True, version_table_schema="gateway")
if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, **options)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, **options)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
