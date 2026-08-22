"""Persist the current LangSmith root trace ID for each durable job attempt.

Revision ID: 0002_job_trace_ids
Revises: 0001_v0
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_job_trace_ids"
down_revision: str | None = "0001_v0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("trace_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "trace_id")
