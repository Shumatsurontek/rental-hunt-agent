"""Replace Telegram delivery state with the Chrome notification outbox.

Revision ID: 0003_chrome_notifications
Revises: 0002_job_trace_ids
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_chrome_notifications"
down_revision: str | None = "0002_job_trace_ids"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column(
            "payload",
            sa.JSON(),
            server_default=sa.text("'{}'::json"),
            nullable=False,
        ),
    )
    op.add_column(
        "notifications",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_column("notifications", "telegram_message_ids")
    op.drop_column("notifications", "sent_at")
    op.add_column(
        "feedback",
        sa.Column(
            "actor",
            sa.String(length=64),
            server_default="chrome_extension",
            nullable=False,
        ),
    )
    op.drop_column("feedback", "telegram_user_id")


def downgrade() -> None:
    op.add_column(
        "feedback",
        sa.Column("telegram_user_id", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.drop_column("feedback", "actor")
    op.add_column(
        "notifications",
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "notifications",
        sa.Column(
            "telegram_message_ids",
            sa.JSON(),
            server_default=sa.text("'[]'::json"),
            nullable=False,
        ),
    )
    op.drop_column("notifications", "delivered_at")
    op.drop_column("notifications", "payload")
