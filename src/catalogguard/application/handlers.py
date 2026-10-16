"""Event handlers. Each one is safe to run again for the same event id."""

import logging
import uuid
from datetime import timedelta

from catalogguard.application.commands import GenerateListing
from catalogguard.application.email_drafts import EmailDraftService
from catalogguard.application.listing import ListingService
from catalogguard.application.ports import EventBus, UnitOfWorkFactory
from catalogguard.domain.crossing import is_cooldown_active, is_downward_crossing
from catalogguard.domain.events import InventoryChanged, LowInventoryDetected
from catalogguard.domain.models import CooldownState
from catalogguard.domain.threshold import resolve_threshold

log = logging.getLogger(__name__)

# Stable namespace so one inventory event cannot mint two low-stock event ids.
_LOW_STOCK_NAMESPACE = uuid.UUID("6f1d8c2a-4b7e-5d19-8c33-1a2b3c4d5e6f")


class ThresholdHandler:
    """InventoryChanged → maybe LowInventoryDetected.

    A crossing that lands inside the cooldown is dropped. It is not deferred.
    See catalogguard.domain.crossing for the trade-off.
    """

    name = "threshold_evaluation"

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        bus: EventBus,
        *,
        default_threshold: int,
        cooldown_seconds: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._bus = bus
        self._default_threshold = default_threshold
        self._cooldown = timedelta(seconds=cooldown_seconds)

    async def handle(self, event: InventoryChanged) -> None:
        async with self._uow_factory() as uow:
            if await uow.processed.seen(event.event_id, self.name):
                return
            await uow.processed.mark(event.event_id, self.name, event.occurred_at)
            product = await uow.products.get(event.sku)
            if product is None:
                await uow.commit()
                return
            inventory = await uow.inventory.get(event.sku, event.store_id)
            store_override = None if inventory is None else inventory.threshold_override
            decision = resolve_threshold(
                global_default=self._default_threshold,
                sku_override=product.threshold_override,
                store_override=store_override,
            )
            cooldown = await uow.cooldowns.get(event.sku, event.store_id)
            crossed = is_downward_crossing(
                previous=event.previous_quantity,
                new=event.new_quantity,
                threshold=decision.value,
            )
            cooling = is_cooldown_active(
                last_triggered_at=None if cooldown is None else cooldown.last_triggered_at,
                now=event.occurred_at,
                cooldown=self._cooldown,
            )
            if crossed and not cooling:
                detected = LowInventoryDetected(
                    event_id=str(uuid.uuid5(_LOW_STOCK_NAMESPACE, event.event_id)),
                    event_type="LowInventoryDetected",
                    occurred_at=event.occurred_at,
                    correlation_id=event.correlation_id,
                    sku=event.sku,
                    store_id=event.store_id,
                    quantity=event.new_quantity,
                    threshold=decision.value,
                    threshold_source=decision.source,
                    previous_quantity=event.previous_quantity,
                    causation_id=event.event_id,
                )
                await self._bus.publish(detected, uow.outbox)
                await uow.cooldowns.upsert(
                    CooldownState(
                        sku=event.sku,
                        store_id=event.store_id,
                        last_triggered_at=event.occurred_at,
                        last_event_id=detected.event_id,
                    )
                )
                log.info(
                    "low_inventory_detected sku=%s store=%s quantity=%s threshold=%s source=%s",
                    event.sku,
                    event.store_id,
                    event.new_quantity,
                    decision.value,
                    decision.source,
                )
            elif crossed:
                log.info(
                    "low_inventory_suppressed sku=%s store=%s reason=cooldown",
                    event.sku,
                    event.store_id,
                )
            await uow.commit()


class ListingRewriteHandler:
    name = "listing_rewrite"

    def __init__(self, listings: ListingService) -> None:
        self._listings = listings

    async def handle(self, event: LowInventoryDetected) -> None:
        await self._listings.generate(
            GenerateListing(
                sku=event.sku,
                store_id=event.store_id,
                trigger_event_id=event.event_id,
                correlation_id=event.correlation_id,
                reason="low_inventory",
            )
        )


class EmailDraftHandler:
    name = "email_draft"

    def __init__(self, emails: EmailDraftService) -> None:
        self._emails = emails

    async def handle(self, event: LowInventoryDetected) -> None:
        await self._emails.generate(event)
