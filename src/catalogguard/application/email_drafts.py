"""Queue a low-stock email draft. This service never sends email.

Targeting is a deliberate simplification. Recipients are opaque customer ids
from RecentInteraction for the same SKU and store, inside a lookback window
measured from the event time. There is no CRM join and no email address here.
One draft is stored per low-stock event. A downstream mailer owns delivery.
"""

import logging
from datetime import timedelta
from uuid import uuid4

from catalogguard.application.ports import Clock, Metrics, UnitOfWorkFactory
from catalogguard.application.prompts import render_email
from catalogguard.domain.errors import NotFoundError
from catalogguard.domain.events import LowInventoryDetected
from catalogguard.domain.models import EmailDraft, RecentInteraction

log = logging.getLogger(__name__)

_INTENT = {"add_to_cart": 2, "view": 1}
_TEMPLATE_VERSION = "email_urgency/v1"
_NOTE = (
    "Deliberate simplification: recipients are opaque customer ids from "
    "RecentInteraction for this SKU and store. This service does not resolve "
    "names or email addresses, and it does not send mail."
)


def select_recipients(rows: list[RecentInteraction]) -> list[RecentInteraction]:
    """One row per customer. Prefer add_to_cart over view, then the later timestamp."""

    best: dict[str, RecentInteraction] = {}
    for row in rows:
        current = best.get(row.customer_id)
        if current is None:
            best[row.customer_id] = row
            continue
        candidate = (_INTENT.get(row.interaction_type, 0), row.timestamp)
        incumbent = (_INTENT.get(current.interaction_type, 0), current.timestamp)
        if candidate > incumbent:
            best[row.customer_id] = row
    return [best[key] for key in sorted(best)]


class EmailDraftService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        template: str,
        *,
        lookback_hours: int,
        metrics: Metrics,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._template = template
        self._lookback = timedelta(hours=lookback_hours)
        self._metrics = metrics
        self._clock = clock

    async def generate(self, event: LowInventoryDetected) -> EmailDraft | None:
        if event.quantity == 0:
            log.info(
                "email_draft_skipped sku=%s store=%s reason=sold_out",
                event.sku,
                event.store_id,
            )
            return None
        dedupe_key = f"low-stock:{event.sku}:{event.store_id}:{event.event_id}"
        async with self._uow_factory() as uow:
            existing = await uow.emails.get_by_dedupe(dedupe_key)
            if existing is not None:
                return existing
            product = await uow.products.get(event.sku)
            store = await uow.stores.get(event.store_id)
            if product is None:
                raise NotFoundError(f"Product {event.sku} not found")
            if store is None:
                raise NotFoundError(f"Store {event.store_id} not found")
            since = event.occurred_at - self._lookback
            rows = await uow.interactions.list_since(
                sku=event.sku,
                store_id=event.store_id,
                since=since,
                until=event.occurred_at,
            )
            recipients = select_recipients(rows)
            subject, body = render_email(
                self._template,
                {
                    "quantity": str(event.quantity),
                    "store_name": store.name,
                    "product_name": product.name,
                    "sku": product.sku,
                },
            )
            draft = EmailDraft(
                id=str(uuid4()),
                sku=event.sku,
                store_id=event.store_id,
                status="queued",
                payload={
                    "template_version": _TEMPLATE_VERSION,
                    "subject": subject,
                    "body": body,
                    "sku": event.sku,
                    "product_name": product.name,
                    "store_id": event.store_id,
                    "store_name": store.name,
                    "quantity_remaining": event.quantity,
                    "quantity_as_of": event.occurred_at.isoformat(),
                    "recipients": [
                        {
                            "customer_id": row.customer_id,
                            "interaction_type": row.interaction_type,
                            "timestamp": row.timestamp.isoformat(),
                        }
                        for row in recipients
                    ],
                    "targeting": "recent_interaction",
                    "targeting_note": _NOTE,
                },
                dedupe_key=dedupe_key,
                trigger_event_id=event.event_id,
                correlation_id=event.correlation_id,
                created_at=self._clock.now(),
            )
            await uow.emails.add(draft)
            await uow.processed.mark(event.event_id, "email_draft", draft.created_at)
            await uow.commit()
        self._metrics.email("queued")
        log.info(
            "email_draft_queued sku=%s store=%s recipients=%s dedupe=%s",
            event.sku,
            event.store_id,
            len(recipients),
            dedupe_key,
        )
        return draft

    async def list(
        self,
        *,
        sku: str | None,
        store_id: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[EmailDraft]:
        async with self._uow_factory() as uow:
            return await uow.emails.list(
                sku=sku,
                store_id=store_id,
                status=status,
                limit=limit,
                offset=offset,
            )
