"""inventory changes: append-only stock and threshold history

Revision ID: 89e567ee29fd
Revises: 5751f906cab6
Create Date: 2026-10-17 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "89e567ee29fd"
down_revision: str | None = "5751f906cab6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inventory_changes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=32), nullable=False),
        sa.Column("previous_quantity", sa.Integer(), nullable=True),
        sa.Column("new_quantity", sa.Integer(), nullable=False),
        sa.Column("previous_threshold_override", sa.Integer(), nullable=True),
        sa.Column("new_threshold_override", sa.Integer(), nullable=True),
        sa.Column("inventory_version", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_inventory_changes_event_id"),
    )
    op.create_index(
        "ix_inventory_changes_sku_store_occurred",
        "inventory_changes",
        ["sku", "store_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_changes_sku_store_occurred", table_name="inventory_changes")
    op.drop_table("inventory_changes")
