"""Request and response models. OpenAPI is generated from these."""

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SKU = r"^[A-Z0-9][A-Z0-9_-]{1,63}$"
_STORE = r"^[a-z][a-z0-9_-]{1,31}$"
_ATTR_KEY = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
# Opaque customer ids, same space as recent_interactions. No email-address shape.
_CUSTOMER_ID = r"^[A-Za-z0-9_.:-]{1,64}$"

AttributeValue = bool | int | float | str


def _check_attributes(value: dict[str, AttributeValue] | None) -> dict[str, AttributeValue] | None:
    if value is None:
        return None
    for key in value:
        if _ATTR_KEY.fullmatch(key) is None:
            raise ValueError(
                f"attribute keys must match ^[a-z][a-z0-9_]{{0,40}}$; rejected {key!r}"
            )
    return value


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(pattern=_SKU, examples=["BKPK-28L-OLV"])
    name: str = Field(min_length=1, max_length=200)
    brand: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=120)
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)
    base_description: str = Field(min_length=1, max_length=8000)
    seo_title: str = Field(default="", max_length=300)
    seo_short_description: str = Field(default="", max_length=4000)
    seo_long_description: str = Field(default="", max_length=8000)
    meta_title: str = Field(default="", max_length=300)
    meta_description: str = Field(default="", max_length=400)
    threshold_override: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("attributes")
    @classmethod
    def attributes_ok(cls, value: dict[str, AttributeValue]) -> dict[str, AttributeValue]:
        checked = _check_attributes(value)
        return {} if checked is None else checked


class ProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    brand: str | None = Field(default=None, min_length=1, max_length=120)
    category: str | None = Field(default=None, min_length=1, max_length=120)
    attributes: dict[str, AttributeValue] | None = None
    base_description: str | None = Field(default=None, min_length=1, max_length=8000)
    seo_title: str | None = Field(default=None, max_length=300)
    seo_short_description: str | None = Field(default=None, max_length=4000)
    seo_long_description: str | None = Field(default=None, max_length=8000)
    meta_title: str | None = Field(default=None, max_length=300)
    meta_description: str | None = Field(default=None, max_length=400)
    threshold_override: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("attributes")
    @classmethod
    def attributes_ok(
        cls, value: dict[str, AttributeValue] | None
    ) -> dict[str, AttributeValue] | None:
        return _check_attributes(value)


class ProductResponse(BaseModel):
    sku: str
    name: str
    brand: str
    category: str
    attributes: dict[str, Any]
    base_description: str
    seo_title: str
    seo_short_description: str
    seo_long_description: str
    meta_title: str
    meta_description: str
    threshold_override: int | None
    created_at: str
    updated_at: str


class StoreCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=_STORE, examples=["nyc"])
    name: str = Field(min_length=1, max_length=200)
    region: str = Field(min_length=1, max_length=120)


class StoreResponse(BaseModel):
    id: str
    name: str
    region: str
    created_at: str


class InventoryUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: int = Field(ge=0, le=1_000_000)
    expected_version: int = Field(ge=0)
    threshold_override: int | None = Field(default=None, ge=0, le=1_000_000)


class InventoryResponse(BaseModel):
    sku: str
    store_id: str
    quantity: int
    version: int
    threshold_override: int | None
    updated_at: str


class InventoryUpdateResponse(InventoryResponse):
    previous_quantity: int | None


class InventoryChangeResponse(BaseModel):
    id: str
    sku: str
    store_id: str
    previous_quantity: int | None
    new_quantity: int
    previous_threshold_override: int | None
    new_threshold_override: int | None
    inventory_version: int
    event_id: str
    occurred_at: str


class InventoryChangePage(BaseModel):
    items: list[InventoryChangeResponse]
    limit: int
    offset: int


class LowStockItem(BaseModel):
    sku: str
    store_id: str
    quantity: int
    inventory_version: int
    threshold: int
    threshold_source: str
    latest_listing_draft_id: str | None
    latest_listing_status: str | None
    latest_email_draft_id: str | None
    latest_email_status: str | None


class LowStockPage(BaseModel):
    items: list[LowStockItem]
    limit: int
    offset: int


class SuppressedCrossingResponse(BaseModel):
    id: str
    sku: str
    store_id: str
    inventory_event_id: str
    occurred_at: str
    previous_quantity: int
    new_quantity: int
    threshold: int
    threshold_source: str
    cooldown_until: str
    reason: str


class SuppressedCrossingPage(BaseModel):
    items: list[SuppressedCrossingResponse]
    limit: int
    offset: int


class DraftCauseResponse(BaseModel):
    draft_id: str
    reason: str
    low_stock_event_id: str | None
    inventory_change: InventoryChangeResponse | None


class ThresholdResponse(BaseModel):
    sku: str
    store_id: str
    effective_threshold: int
    source: str
    global_default: int
    sku_override: int | None
    sku_store_override: int | None
    comparison: str
    cooldown_seconds: int


class PublishListing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)


class ListingRollback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_version: int = Field(ge=1)
    expected_version: int = Field(ge=0)


class StoreListingRevisionResponse(BaseModel):
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
    published_at: str


class StoreListingResponse(BaseModel):
    sku: str
    store_id: str
    version: int
    title: str
    short_description: str
    long_description: str
    meta_title: str
    meta_description: str
    published_draft_id: str
    published_at: str


class ListingDraftResponse(BaseModel):
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
    trigger_event_id: str
    correlation_id: str
    created_at: str


class EmailDraftResponse(BaseModel):
    id: str
    sku: str
    store_id: str
    status: str
    payload: dict[str, Any]
    dedupe_key: str
    trigger_event_id: str
    correlation_id: str
    created_at: str


class EmailSuppressionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(pattern=_CUSTOMER_ID, examples=["cus_1842"])
    reason: str = Field(min_length=1, max_length=200, examples=["unsubscribed"])


class EmailSuppressionResponse(BaseModel):
    customer_id: str
    reason: str
    created_at: str


class EmailHandoffResponse(BaseModel):
    id: str
    email_draft_id: str
    status: str
    payload: dict[str, Any]
    suppressed_customer_ids: list[str]
    created_at: str


class DeadLetterResponse(BaseModel):
    id: str
    event_id: str
    event_type: str
    handler: str
    payload: dict[str, Any]
    error: str
    attempts: int
    created_at: str
    redrive_count: int
    last_redrive_status: str | None
    last_redriven_at: str | None


class OutboxSummaryResponse(BaseModel):
    unpublished: int
    claimed: int
    oldest_unpublished_age_seconds: int | None


class DeadLetterRedriveResponse(BaseModel):
    dead_letter_id: str
    handler: str
    result: str
    replayed: bool
    redrive_id: str


class ProductPage(BaseModel):
    items: list[ProductResponse]
    limit: int
    offset: int


class StorePage(BaseModel):
    items: list[StoreResponse]
    limit: int
    offset: int


class InventoryPage(BaseModel):
    items: list[InventoryResponse]
    limit: int
    offset: int


class ListingDraftPage(BaseModel):
    items: list[ListingDraftResponse]
    limit: int
    offset: int


class StoreListingRevisionPage(BaseModel):
    items: list[StoreListingRevisionResponse]
    limit: int
    offset: int


class EmailDraftPage(BaseModel):
    items: list[EmailDraftResponse]
    limit: int
    offset: int


class EmailSuppressionPage(BaseModel):
    items: list[EmailSuppressionResponse]
    limit: int
    offset: int


class EmailHandoffPage(BaseModel):
    items: list[EmailHandoffResponse]
    limit: int
    offset: int


class DeadLetterPage(BaseModel):
    items: list[DeadLetterResponse]
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str
    message: str
    current_version: int | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
