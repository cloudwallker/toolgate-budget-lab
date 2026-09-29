"""Initial gateway tables, constraints and occupancy index."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision = "0001_gateway"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "principals",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("allowed_tools", pg.ARRAY(sa.String(64)), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        sa.Column("max_inflight", sa.Integer(), nullable=False),
        sa.CheckConstraint("daily_limit >= 0 AND max_inflight > 0"),
        schema="gateway",
    )
    op.create_table(
        "daily_usage",
        sa.Column(
            "principal_id", sa.String(64), sa.ForeignKey("gateway.principals.id"), primary_key=True
        ),
        sa.Column("utc_date", sa.Date(), primary_key=True),
        sa.Column("used", sa.Integer(), nullable=False),
        sa.CheckConstraint("used >= 0"),
        schema="gateway",
    )
    op.create_table(
        "calls",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "principal_id", sa.String(64), sa.ForeignKey("gateway.principals.id"), nullable=False
        ),
        sa.Column("request_key", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("tool", sa.String(64), nullable=False),
        sa.Column("quota_date", sa.Date(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("slot_held", sa.Boolean(), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", pg.JSONB()),
        sa.Column("evidence", pg.JSONB()),
        sa.Column("decision_ms", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.UniqueConstraint("principal_id", "request_key"),
        sa.CheckConstraint(
            "state IN ('reserved','dispatching','running','unknown','succeeded','failed','aborted')"
        ),
        sa.CheckConstraint("slot_held = (state IN ('reserved','dispatching','running','unknown'))"),
        schema="gateway",
    )
    op.create_index(
        "ix_calls_held_principal",
        "calls",
        ["principal_id"],
        schema="gateway",
        postgresql_where=sa.text("slot_held"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("principal_id", sa.String(64)),
        sa.Column("call_id", pg.UUID(as_uuid=True)),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        schema="gateway",
    )


def downgrade():
    op.drop_table("audit_events", schema="gateway")
    op.drop_index("ix_calls_held_principal", table_name="calls", schema="gateway")
    op.drop_table("calls", schema="gateway")
    op.drop_table("daily_usage", schema="gateway")
    op.drop_table("principals", schema="gateway")
