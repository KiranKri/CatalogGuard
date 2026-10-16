"""Domain objects to JSON-ready dicts."""

from catalogguard.application.queries import DeadLetterView
from catalogguard.application.redrive import RedriveResult
from catalogguard.domain.models import (
    EmailDraft,
    EmailHandoff,
    EmailSuppression,
    InventoryLevel,
    ListingDraft,
    Product,
    Store,
    StoreListing,
)


def product_out(product: Product) -> dict[str, object]:
    return {
        "sku": product.sku,
        "name": product.name,
        "brand": product.brand,
        "category": product.category,
        "attributes": dict(product.attributes),
        "base_description": product.base_description,
        "seo_title": product.seo_title,
        "seo_short_description": product.seo_short_description,
        "seo_long_description": product.seo_long_description,
        "meta_title": product.meta_title,
        "meta_description": product.meta_description,
        "threshold_override": product.threshold_override,
        "created_at": product.created_at.isoformat(),
        "updated_at": product.updated_at.isoformat(),
    }


def store_out(store: Store) -> dict[str, object]:
    return {
        "id": store.id,
        "name": store.name,
        "region": store.region,
        "created_at": store.created_at.isoformat(),
    }


def inventory_out(level: InventoryLevel) -> dict[str, object]:
    return {
        "sku": level.sku,
        "store_id": level.store_id,
        "quantity": level.quantity,
        "version": level.version,
        "threshold_override": level.threshold_override,
        "updated_at": level.updated_at.isoformat(),
    }


def draft_out(draft: ListingDraft) -> dict[str, object]:
    return {
        "id": draft.id,
        "sku": draft.sku,
        "store_id": draft.store_id,
        "version": draft.version,
        "prompt_version": draft.prompt_version,
        "model": draft.model,
        "status": draft.status,
        "reason": draft.reason,
        "title": draft.title,
        "short_description": draft.short_description,
        "long_description": draft.long_description,
        "meta_title": draft.meta_title,
        "meta_description": draft.meta_description,
        "cited_attributes": list(draft.cited_attributes),
        "violations": list(draft.violations),
        "trigger_event_id": draft.trigger_event_id,
        "correlation_id": draft.correlation_id,
        "created_at": draft.created_at.isoformat(),
    }


def store_listing_out(listing: StoreListing) -> dict[str, object]:
    return {
        "sku": listing.sku,
        "store_id": listing.store_id,
        "version": listing.version,
        "title": listing.title,
        "short_description": listing.short_description,
        "long_description": listing.long_description,
        "meta_title": listing.meta_title,
        "meta_description": listing.meta_description,
        "published_draft_id": listing.published_draft_id,
        "published_at": listing.published_at.isoformat(),
    }


def email_out(draft: EmailDraft) -> dict[str, object]:
    return {
        "id": draft.id,
        "sku": draft.sku,
        "store_id": draft.store_id,
        "status": draft.status,
        "payload": dict(draft.payload),
        "dedupe_key": draft.dedupe_key,
        "trigger_event_id": draft.trigger_event_id,
        "correlation_id": draft.correlation_id,
        "created_at": draft.created_at.isoformat(),
    }


def suppression_out(suppression: EmailSuppression) -> dict[str, object]:
    return {
        "customer_id": suppression.customer_id,
        "reason": suppression.reason,
        "created_at": suppression.created_at.isoformat(),
    }


def handoff_out(handoff: EmailHandoff) -> dict[str, object]:
    return {
        "id": handoff.id,
        "email_draft_id": handoff.email_draft_id,
        "status": handoff.status,
        "payload": dict(handoff.payload),
        "suppressed_customer_ids": list(handoff.suppressed_customer_ids),
        "created_at": handoff.created_at.isoformat(),
    }


def dead_letter_out(view: DeadLetterView) -> dict[str, object]:
    letter = view.letter
    return {
        "id": letter.id,
        "event_id": letter.event_id,
        "event_type": letter.event_type,
        "handler": letter.handler,
        "payload": dict(letter.payload),
        "error": letter.error,
        "attempts": letter.attempts,
        "created_at": letter.created_at.isoformat(),
        "redrive_count": view.redrive_count,
        "last_redrive_status": view.last_redrive_status,
        "last_redriven_at": (
            None if view.last_redriven_at is None else view.last_redriven_at.isoformat()
        ),
    }


def redrive_out(result: RedriveResult) -> dict[str, object]:
    return {
        "dead_letter_id": result.dead_letter_id,
        "handler": result.handler,
        "result": result.result,
        "replayed": result.replayed,
        "redrive_id": result.redrive_id,
    }


def page(items: list[object], *, limit: int, offset: int) -> dict[str, object]:
    return {"items": items, "limit": limit, "offset": offset}
