"""Domain events. Payloads are plain values so they can be stored and re-delivered."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class InventoryChanged:
    event_id: str
    event_type: str
    occurred_at: datetime
    correlation_id: str
    sku: str
    store_id: str
    previous_quantity: int | None
    new_quantity: int
    inventory_version: int


@dataclass(frozen=True)
class LowInventoryDetected:
    event_id: str
    event_type: str
    occurred_at: datetime
    correlation_id: str
    sku: str
    store_id: str
    quantity: int
    threshold: int
    threshold_source: str
    previous_quantity: int | None
    causation_id: str


DomainEvent = InventoryChanged | LowInventoryDetected
