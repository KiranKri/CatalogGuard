"""Inputs the API builds for use cases."""

from dataclasses import dataclass


@dataclass(frozen=True)
class UpsertInventory:
    sku: str
    store_id: str
    quantity: int
    expected_version: int
    threshold_override: int | None
    threshold_override_provided: bool
    idempotency_key: str
    request_hash: str
    correlation_id: str


@dataclass(frozen=True)
class InventoryUpdateResult:
    body: dict[str, object]
    status_code: int
    replayed: bool


@dataclass(frozen=True)
class GenerateListing:
    sku: str
    store_id: str
    trigger_event_id: str
    correlation_id: str
    reason: str
