"""Entities and records. Persistence mapping lives in infrastructure."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Store:
    id: str
    name: str
    region: str
    created_at: datetime


@dataclass(frozen=True)
class Product:
    sku: str
    name: str
    brand: str
    category: str
    attributes: dict[str, object]
    base_description: str
    seo_title: str
    seo_short_description: str
    seo_long_description: str
    meta_title: str
    meta_description: str
    threshold_override: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class InventoryLevel:
    sku: str
    store_id: str
    quantity: int
    threshold_override: int | None
    updated_at: datetime
    version: int


@dataclass(frozen=True)
class ListingDraft:
    id: str
    sku: str
    store_id: str
    version: int
    prompt_version: str
    model: str
    status: str
    reason: str
    title: str
    short_description: str
    long_description: str
    meta_title: str
    meta_description: str
    cited_attributes: list[str]
    violations: list[str]
    raw_output: str
    trigger_event_id: str
    correlation_id: str
    created_at: datetime


@dataclass(frozen=True)
class StoreListing:
    """Live store-scoped copy. Written only when an operator publishes a draft."""

    sku: str
    store_id: str
    version: int
    title: str
    short_description: str
    long_description: str
    meta_title: str
    meta_description: str
    published_draft_id: str
    published_at: datetime


@dataclass(frozen=True)
class StoreListingRevision:
    """Append-only copy of the customer-facing text one publish made live."""

    id: str
    sku: str
    store_id: str
    version: int
    published_draft_id: str
    title: str
    short_description: str
    long_description: str
    meta_title: str
    meta_description: str
    published_at: datetime


@dataclass(frozen=True)
class EmailDraft:
    id: str
    sku: str
    store_id: str
    status: str
    payload: dict[str, object]
    dedupe_key: str
    trigger_event_id: str
    correlation_id: str
    created_at: datetime


@dataclass(frozen=True)
class RecentInteraction:
    id: str
    customer_id: str
    sku: str
    store_id: str
    interaction_type: str
    timestamp: datetime


@dataclass(frozen=True)
class IdempotencyRecord:
    key: str
    request_hash: str
    response_code: int
    response_body: dict[str, object]
    created_at: datetime


@dataclass(frozen=True)
class CooldownState:
    sku: str
    store_id: str
    last_triggered_at: datetime
    last_event_id: str


@dataclass(frozen=True)
class EmailSuppression:
    """An opaque customer id that must not appear on a mailer handoff."""

    customer_id: str
    reason: str
    created_at: datetime


@dataclass(frozen=True)
class EmailHandoff:
    """The handoff worker's one decision for an email draft. Nothing is sent."""

    id: str
    email_draft_id: str
    status: str
    payload: dict[str, object]
    suppressed_customer_ids: list[str]
    created_at: datetime


@dataclass(frozen=True)
class DeadLetter:
    id: str
    event_id: str
    event_type: str
    handler: str
    payload: dict[str, object]
    error: str
    attempts: int
    created_at: datetime


@dataclass(frozen=True)
class DeadLetterRedrive:
    """One operator attempt to rerun a dead letter's handler. Rows are never updated."""

    id: str
    dead_letter_id: str
    status: str
    error: str
    created_at: datetime
