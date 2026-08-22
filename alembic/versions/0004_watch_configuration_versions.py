"""Version watcher configurations without erasing prior listing history.

Revision ID: 0004_watch_revisions
Revises: 0003_chrome_notifications
Create Date: 2026-08-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_watch_revisions"
down_revision: str | None = "0003_chrome_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "watches",
        sa.Column("configuration_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "listings",
        sa.Column("configuration_version", sa.Integer(), server_default="1", nullable=False),
    )
    op.drop_constraint("uq_listing_source_id", "listings", type_="unique")
    op.create_unique_constraint(
        "uq_listing_source_id",
        "listings",
        ["watch_id", "configuration_version", "source", "source_listing_id"],
    )
    op.alter_column("watches", "configuration_version", server_default=None)
    op.alter_column("listings", "configuration_version", server_default=None)


def downgrade() -> None:
    op.drop_constraint("uq_listing_source_id", "listings", type_="unique")
    op.create_unique_constraint(
        "uq_listing_source_id",
        "listings",
        ["watch_id", "source", "source_listing_id"],
    )
    op.drop_column("listings", "configuration_version")
    op.drop_column("watches", "configuration_version")
