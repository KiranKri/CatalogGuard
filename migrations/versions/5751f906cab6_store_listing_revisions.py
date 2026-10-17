"""store listing revisions: append-only live listing history

Revision ID: 5751f906cab6
Revises: c81f0f63dcdc
Create Date: 2026-10-17 03:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5751f906cab6"
down_revision: str | None = "c81f0f63dcdc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "store_listing_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("published_draft_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("short_description", sa.Text(), nullable=False),
        sa.Column("long_description", sa.Text(), nullable=False),
        sa.Column("meta_title", sa.Text(), nullable=False),
        sa.Column("meta_description", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["published_draft_id"],
            ["listing_drafts.id"],
            name="fk_store_listing_revisions_published_draft_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "sku", "store_id", "version", name="uq_store_listing_revisions_version"
        ),
    )
    op.create_index(
        "ix_store_listing_revisions_published",
        "store_listing_revisions",
        ["sku", "store_id", "published_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_store_listing_revisions_published", table_name="store_listing_revisions")
    op.drop_table("store_listing_revisions")
