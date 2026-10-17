"""dead letter redrive claims: one in-flight redrive per letter

Revision ID: 6e218e07a396
Revises: b19fd5f52b4b
Create Date: 2026-10-17 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6e218e07a396"
down_revision: str | None = "b19fd5f52b4b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dead_letter_redrive_claims",
        sa.Column("dead_letter_id", sa.String(length=36), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dead_letter_id"],
            ["dead_letters.id"],
            name="fk_dead_letter_redrive_claims_dead_letter_id",
        ),
        sa.PrimaryKeyConstraint("dead_letter_id"),
    )


def downgrade() -> None:
    op.drop_table("dead_letter_redrive_claims")
