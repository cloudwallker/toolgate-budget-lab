from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class MockBase(DeclarativeBase):
    pass


class Operation(MockBase):
    __tablename__ = "operations"
    __table_args__ = (
        CheckConstraint("state IN ('running', 'succeeded', 'failed')", name="valid_state"),
        CheckConstraint(
            "(state = 'running' AND completed_at IS NULL AND result IS NULL) OR "
            "(state IN ('succeeded', 'failed') AND completed_at IS NOT NULL)",
            name="state_completion",
        ),
        {"schema": "tools"},
    )

    operation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    tool: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
