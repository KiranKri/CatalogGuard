"""JSON-safe event payload mapping."""

from dataclasses import asdict, fields
from datetime import UTC, datetime

from catalogguard.domain.events import DomainEvent, InventoryChanged, LowInventoryDetected

_TYPES: dict[str, type[DomainEvent]] = {
    "InventoryChanged": InventoryChanged,
    "LowInventoryDetected": LowInventoryDetected,
}


def dump_event(event: DomainEvent) -> dict[str, object]:
    raw: dict[str, object] = asdict(event)
    for key, value in raw.items():
        if isinstance(value, datetime):
            raw[key] = value.isoformat()
    return raw


def load_event(payload: dict[str, object]) -> DomainEvent:
    data = dict(payload)
    occurred = data.get("occurred_at")
    if isinstance(occurred, str):
        parsed = datetime.fromisoformat(occurred)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        data["occurred_at"] = parsed
    event_type = str(data.get("event_type", ""))
    cls = _TYPES.get(event_type)
    if cls is None:
        raise ValueError(f"Unknown event type: {event_type}")
    kwargs = {field.name: data[field.name] for field in fields(cls)}
    return cls(**kwargs)
