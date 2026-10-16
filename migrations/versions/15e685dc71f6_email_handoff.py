"""email handoff: suppressions and handoff decisions

Revision ID: 15e685dc71f6
Revises: 0d865ba6ff4d
Create Date: 2026-10-17 01:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "15e685dc71f6"
down_revision: str | None = "0d865ba6ff4d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "email_suppressions",
        sa.Column("customer_id", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("customer_id"),
    )
    op.create_table(
        "email_handoffs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email_draft_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", _json(), nullable=False),
        sa.Column("suppressed_customer_ids", _json(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('handed_off', 'suppressed', 'expired')",
            name="ck_email_handoff_status",
        ),
        sa.ForeignKeyConstraint(["email_draft_id"], ["email_drafts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email_draft_id", name="uq_email_handoffs_email_draft_id"),
    )
    op.create_index("ix_email_handoffs_status", "email_handoffs", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_email_handoffs_status", table_name="email_handoffs")
    op.drop_table("email_handoffs")
    op.drop_table("email_suppressions")
