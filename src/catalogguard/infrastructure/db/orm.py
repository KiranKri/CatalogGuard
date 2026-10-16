"""Relational model. Schema is created on startup with metadata.create_all."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _json() -> JSON:
    return JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class StoreORM(Base):
    __tablename__ = "stores"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    region: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProductORM(Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    brand: Mapped[str] = mapped_column(String(120), nullable=False)
    category: Mapped[str] = mapped_column(String(120), nullable=False)
    attributes: Mapped[dict] = mapped_column(_json(), nullable=False)
    base_description: Mapped[str] = mapped_column(Text, nullable=False)
    seo_title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    seo_short_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    seo_long_description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    meta_title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    meta_description: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    threshold_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InventoryLevelORM(Base):
    __tablename__ = "inventory_levels"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_inventory_quantity_nonneg"),
        CheckConstraint("version >= 1", name="ck_inventory_version_pos"),
    )

    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"), primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    threshold_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class ListingDraftORM(Base):
    __tablename__ = "listing_drafts"
    __table_args__ = (
        UniqueConstraint("sku", "store_id", "version", name="uq_listing_draft_version"),
        CheckConstraint(
            "status in ('ready', 'validation_failed', 'published')",
            name="ck_listing_draft_status",
        ),
        Index("ix_listing_drafts_sku_store", "sku", "store_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"), nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    short_description: Mapped[str] = mapped_column(Text, nullable=False)
    long_description: Mapped[str] = mapped_column(Text, nullable=False)
    meta_title: Mapped[str] = mapped_column(Text, nullable=False)
    meta_description: Mapped[str] = mapped_column(Text, nullable=False)
    cited_attributes: Mapped[list] = mapped_column(_json(), nullable=False)
    violations: Mapped[list] = mapped_column(_json(), nullable=False)
    raw_output: Mapped[str] = mapped_column(Text, nullable=False)
    trigger_event_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StoreListingORM(Base):
    __tablename__ = "store_listings"
    __table_args__ = (CheckConstraint("version >= 1", name="ck_store_listing_version"),)

    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"), primary_key=True)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    short_description: Mapped[str] = mapped_column(Text, nullable=False)
    long_description: Mapped[str] = mapped_column(Text, nullable=False)
    meta_title: Mapped[str] = mapped_column(Text, nullable=False)
    meta_description: Mapped[str] = mapped_column(Text, nullable=False)
    published_draft_id: Mapped[str] = mapped_column(
        ForeignKey("listing_drafts.id"),
        nullable=False,
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LlmUsageORM(Base):
    """Calls reserved for one SKU on one UTC day. The cap is enforced in the write."""

    __tablename__ = "llm_usage"

    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    usage_date: Mapped[str] = mapped_column(String(10), primary_key=True)
    call_count: Mapped[int] = mapped_column(Integer, nullable=False)


class EmailDraftORM(Base):
    __tablename__ = "email_drafts"
    __table_args__ = (
        CheckConstraint("status in ('queued')", name="ck_email_draft_status"),
        Index("ix_email_drafts_sku_store", "sku", "store_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"), nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(_json(), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    trigger_event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EmailSuppressionORM(Base):
    __tablename__ = "email_suppressions"

    customer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EmailHandoffORM(Base):
    """One row per decided draft. The unique draft id is the idempotency record."""

    __tablename__ = "email_handoffs"
    __table_args__ = (
        UniqueConstraint("email_draft_id", name="uq_email_handoffs_email_draft_id"),
        CheckConstraint(
            "status in ('handed_off', 'suppressed', 'expired')",
            name="ck_email_handoff_status",
        ),
        Index("ix_email_handoffs_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    email_draft_id: Mapped[str] = mapped_column(ForeignKey("email_drafts.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(_json(), nullable=False)
    suppressed_customer_ids: Mapped[list] = mapped_column(_json(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RecentInteractionORM(Base):
    __tablename__ = "recent_interactions"
    __table_args__ = (
        CheckConstraint(
            "interaction_type in ('view', 'add_to_cart')",
            name="ck_interaction_type",
        ),
        Index("ix_interactions_sku_store_time", "sku", "store_id", "timestamp"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sku: Mapped[str] = mapped_column(ForeignKey("products.sku"), nullable=False)
    store_id: Mapped[str] = mapped_column(ForeignKey("stores.id"), nullable=False)
    interaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        "timestamp",
        DateTime(timezone=True),
        nullable=False,
    )


class IdempotencyORM(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(_json(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OutboxORM(Base):
    __tablename__ = "event_outbox"
    __table_args__ = (
        Index("ix_outbox_published", "published_at"),
        Index("ix_outbox_claim", "published_at", "claimed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(_json(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)


class ProcessedEventORM(Base):
    __tablename__ = "processed_events"

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    handler: Mapped[str] = mapped_column(String(64), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CooldownORM(Base):
    __tablename__ = "low_stock_cooldowns"

    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_event_id: Mapped[str] = mapped_column(String(36), nullable=False)


class DeadLetterORM(Base):
    __tablename__ = "dead_letters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    handler: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(_json(), nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeadLetterRedriveORM(Base):
    """Operator redrive attempts. Any succeeded row is the idempotency record for its letter."""

    __tablename__ = "dead_letter_redrives"
    __table_args__ = (
        CheckConstraint(
            "status in ('succeeded', 'failed')",
            name="ck_dead_letter_redrive_status",
        ),
        Index("ix_dead_letter_redrives_dead_letter_id", "dead_letter_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    dead_letter_id: Mapped[str] = mapped_column(
        ForeignKey("dead_letters.id", name="fk_dead_letter_redrives_dead_letter_id"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
