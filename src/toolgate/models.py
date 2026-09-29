"""Gateway-owned PostgreSQL tables; occupancy is derived from held calls."""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Principal(Base):
    __tablename__ = "principals"
    __table_args__ = (
        CheckConstraint("daily_limit >= 0 AND max_inflight > 0"),
        {"schema": "gateway"},
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    allowed_tools: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False)
    daily_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    max_inflight: Mapped[int] = mapped_column(Integer, nullable=False)


class DailyUsage(Base):
    __tablename__ = "daily_usage"
    __table_args__ = (CheckConstraint("used >= 0"), {"schema": "gateway"})
    principal_id: Mapped[str] = mapped_column(ForeignKey("gateway.principals.id"), primary_key=True)
    utc_date: Mapped[date] = mapped_column(Date, primary_key=True)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Call(Base):
    __tablename__ = "calls"
    __table_args__ = (
        UniqueConstraint("principal_id", "request_key"),
        CheckConstraint(
            "state IN ('reserved','dispatching','running','unknown','succeeded','failed','aborted')"
        ),
        CheckConstraint("slot_held = (state IN ('reserved','dispatching','running','unknown'))"),
        Index("ix_calls_held_principal", "principal_id", postgresql_where="slot_held"),
        {"schema": "gateway"},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    principal_id: Mapped[str] = mapped_column(ForeignKey("gateway.principals.id"))
    request_key: Mapped[UUID] = mapped_column(nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    quota_date: Mapped[date] = mapped_column(Date, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    slot_held: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    decision_ms: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = ({"schema": "gateway"},)
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    principal_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    call_id: Mapped[UUID | None] = mapped_column(nullable=True)
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )
