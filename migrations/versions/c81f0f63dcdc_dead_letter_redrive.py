"""dead letter redrive attempts

Revision ID: c81f0f63dcdc
Revises: 15e685dc71f6
Create Date: 2026-10-17 02:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c81f0f63dcdc"
down_revision: str | None = "15e685dc71f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dead_letter_redrives",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("dead_letter_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('succeeded', 'failed')",
            name="ck_dead_letter_redrive_status",
        ),
        sa.ForeignKeyConstraint(
            ["dead_letter_id"],
            ["dead_letters.id"],
            name="fk_dead_letter_redrives_dead_letter_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dead_letter_redrives_dead_letter_id",
        "dead_letter_redrives",
        ["dead_letter_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_dead_letter_redrives_dead_letter_id", table_name="dead_letter_redrives")
    op.drop_table("dead_letter_redrives")
