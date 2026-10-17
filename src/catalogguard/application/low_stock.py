"""Read-only low-stock views: what is low in a store, and why a draft exists.

The board resolves each row with the same `resolve_threshold` the crossing
handler uses (store override, else SKU override, else the global default) and
keeps `quantity < threshold`. The three levels live in two tables plus
configuration, so the comparison happens in Python, not SQL.

A low-inventory draft's trigger is the LowInventoryDetected event id. Its
outbox payload carries `causation_id`, the InventoryChanged event id, which is
also the `event_id` on the inventory_changes row that write recorded. A missing
or mismatched link gives a null change, never an error.
"""

from dataclasses import dataclass

from catalogguard.application.ports import UnitOfWorkFactory
from catalogguard.domain.errors import NotFoundError
from catalogguard.domain.models import InventoryChange
from catalogguard.domain.threshold import resolve_threshold

LOW_INVENTORY = "low_inventory"


@dataclass(frozen=True)
class LowStockRow:
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


@dataclass(frozen=True)
class DraftCause:
    draft_id: str
    reason: str
    low_stock_event_id: str | None
    inventory_change: InventoryChange | None


class LowStockService:
    def __init__(self, uow_factory: UnitOfWorkFactory, *, default_threshold: int) -> None:
        self._uow_factory = uow_factory
        self._default_threshold = default_threshold

    async def board(self, *, store_id: str, limit: int, offset: int) -> list[LowStockRow]:
        async with self._uow_factory() as uow:
            if await uow.stores.get(store_id) is None:
                raise NotFoundError(f"Store {store_id} not found")
            levels = await uow.inventory.for_store(store_id)
            sku_overrides = await uow.products.threshold_overrides([lv.sku for lv in levels])
            low = []
            for level in levels:
                decision = resolve_threshold(
                    global_default=self._default_threshold,
                    sku_override=sku_overrides.get(level.sku),
                    store_override=level.threshold_override,
                )
                if level.quantity < decision.value:
                    low.append((level, decision))
            low.sort(key=lambda pair: pair[0].sku)
            window = low[offset : offset + limit]
            skus = [level.sku for level, _ in window]
            listings = await uow.drafts.latest_for_store(store_id, skus)
            emails = await uow.emails.latest_for_store(store_id, skus)
        rows: list[LowStockRow] = []
        for level, decision in window:
            listing = listings.get(level.sku)
            email = emails.get(level.sku)
            rows.append(
                LowStockRow(
                    sku=level.sku,
                    store_id=level.store_id,
                    quantity=level.quantity,
                    inventory_version=level.version,
                    threshold=decision.value,
                    threshold_source=decision.source,
                    latest_listing_draft_id=None if listing is None else listing.id,
                    latest_listing_status=None if listing is None else listing.status,
                    latest_email_draft_id=None if email is None else email.id,
                    latest_email_status=None if email is None else email.status,
                )
            )
        return rows

    async def draft_cause(self, draft_id: str) -> DraftCause:
        async with self._uow_factory() as uow:
            draft = await uow.drafts.get(draft_id)
            if draft is None:
                raise NotFoundError(f"Listing draft {draft_id} not found")
            if draft.reason != LOW_INVENTORY:
                return DraftCause(
                    draft_id=draft.id,
                    reason=draft.reason,
                    low_stock_event_id=None,
                    inventory_change=None,
                )
            change: InventoryChange | None = None
            payload = await uow.outbox.payload_by_event_id(draft.trigger_event_id)
            causation_id = None if payload is None else payload.get("causation_id")
            if isinstance(causation_id, str):
                change = await uow.inventory_changes.get_by_event(causation_id)
            if change is not None and (
                change.sku != draft.sku or change.store_id != draft.store_id
            ):
                change = None
        return DraftCause(
            draft_id=draft.id,
            reason=draft.reason,
            low_stock_event_id=draft.trigger_event_id,
            inventory_change=change,
        )
