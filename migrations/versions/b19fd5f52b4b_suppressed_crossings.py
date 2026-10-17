"""suppressed crossings: downward crossings dropped inside the cooldown

Revision ID: b19fd5f52b4b
Revises: 89e567ee29fd
Create Date: 2026-10-17 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b19fd5f52b4b"
down_revision: str | None = "89e567ee29fd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "suppressed_crossings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("store_id", sa.String(length=32), nullable=False),
        sa.Column("inventory_event_id", sa.String(length=36), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("previous_quantity", sa.Integer(), nullable=False),
        sa.Column("new_quantity", sa.Integer(), nullable=False),
        sa.Column("threshold", sa.Integer(), nullable=False),
        sa.Column("threshold_source", sa.String(length=32), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.CheckConstraint("reason in ('cooldown')", name="ck_suppressed_crossing_reason"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "inventory_event_id", name="uq_suppressed_crossings_inventory_event_id"
        ),
    )
    op.create_index(
        "ix_suppressed_crossings_sku_store_occurred",
        "suppressed_crossings",
        ["sku", "store_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_suppressed_crossings_sku_store_occurred", table_name="suppressed_crossings")
    op.drop_table("suppressed_crossings")
