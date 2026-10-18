"""Add durable Postgres jobs table.

Revision ID: 48c62ca2a741
Revises: b206b9bedf2c
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "48c62ca2a741"
down_revision: str | None = "b206b9bedf2c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("result", json_type, nullable=True),
        sa.Column("error", json_type, nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("locked_by", sa.String(length=128), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("callback_url", sa.Text(), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_type", "jobs", ["type"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_available_at", "jobs", ["available_at"])
    op.create_index("ix_jobs_claim", "jobs", ["status", "available_at", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_index("ix_jobs_available_at", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_index("ix_jobs_type", table_name="jobs")
    op.drop_table("jobs")
