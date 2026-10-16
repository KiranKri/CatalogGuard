"""REST routes. Handlers stay thin and call application services."""

import hashlib
import json
import re
from uuid import uuid4

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from catalogguard.application.commands import GenerateListing, UpsertInventory
from catalogguard.domain.errors import InvalidRequestError
from catalogguard.domain.models import Product
from catalogguard.infrastructure.api.schemas import (
    DeadLetterPage,
    DeadLetterRedriveResponse,
    EmailDraftPage,
    EmailHandoffPage,
    EmailSuppressionCreate,
    EmailSuppressionPage,
    EmailSuppressionResponse,
    ErrorResponse,
    InventoryPage,
    InventoryResponse,
    InventoryUpdateResponse,
    InventoryUpsert,
    ListingDraftPage,
    ListingDraftResponse,
    ProductCreate,
    ProductPage,
    ProductResponse,
    ProductUpdate,
    PublishListing,
    StoreCreate,
    StoreListingResponse,
    StorePage,
    StoreResponse,
    ThresholdResponse,
)
from catalogguard.infrastructure.api.serialize import (
    dead_letter_out,
    draft_out,
    email_out,
    handoff_out,
    inventory_out,
    page,
    product_out,
    redrive_out,
    store_listing_out,
    store_out,
    suppression_out,
)
from catalogguard.infrastructure.container import Container
from catalogguard.infrastructure.metrics import METRICS_CONTENT_TYPE, render_metrics

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_.:-]{8,200}$")

router = APIRouter(prefix="/api/v1")
ops_router = APIRouter()


def _container(request: Request) -> Container:
    return request.app.state.container


def _correlation(request: Request) -> str:
    value = getattr(request.state, "correlation_id", None)
    return value or str(uuid4())


def _inventory_hash(sku: str, store_id: str, body: InventoryUpsert, fields: set[str]) -> str:
    payload = {
        "sku": sku,
        "store_id": store_id,
        "quantity": body.quantity,
        "expected_version": body.expected_version,
        "threshold_override": (
            body.threshold_override if "threshold_override" in fields else "__unchanged__"
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@ops_router.get("/health", tags=["ops"])
async def health(request: Request) -> dict[str, str]:
    settings = request.app.state.settings
    return {
        "status": "ok",
        "llm_provider": settings.llm_provider,
        "event_dispatch": settings.event_dispatch,
    }


@ops_router.get("/metrics", tags=["ops"], response_class=Response)
async def metrics() -> Response:
    return Response(content=render_metrics(), media_type=METRICS_CONTENT_TYPE)


@router.post("/products", response_model=ProductResponse, status_code=201, tags=["products"])
async def create_product(body: ProductCreate, request: Request) -> dict[str, object]:
    container = _container(request)
    now = container.clock.now()
    product = Product(
        sku=body.sku,
        name=body.name,
        brand=body.brand,
        category=body.category,
        attributes=dict(body.attributes),
        base_description=body.base_description,
        seo_title=body.seo_title,
        seo_short_description=body.seo_short_description,
        seo_long_description=body.seo_long_description,
        meta_title=body.meta_title,
        meta_description=body.meta_description,
        threshold_override=body.threshold_override,
        created_at=now,
        updated_at=now,
    )
    created = await container.products.create(product)
    return product_out(created)


@router.get("/products", response_model=ProductPage, tags=["products"])
async def list_products(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    products = await _container(request).products.list(limit=limit, offset=offset)
    return page([product_out(item) for item in products], limit=limit, offset=offset)


@router.get("/products/{sku}", response_model=ProductResponse, tags=["products"])
async def get_product(sku: str, request: Request) -> dict[str, object]:
    return product_out(await _container(request).products.get(sku))


@router.patch("/products/{sku}", response_model=ProductResponse, tags=["products"])
async def update_product(sku: str, body: ProductUpdate, request: Request) -> dict[str, object]:
    changes: dict[str, object] = {}
    for key in body.model_fields_set:
        value = getattr(body, key)
        if key != "threshold_override" and value is None:
            raise InvalidRequestError(f"{key} cannot be null")
        changes[key] = value
    if not changes:
        raise InvalidRequestError("No fields to update")
    updated = await _container(request).products.update(sku, changes)
    return product_out(updated)


@router.delete(
    "/products/{sku}",
    status_code=204,
    tags=["products"],
    responses={409: {"model": ErrorResponse}},
)
async def delete_product(sku: str, request: Request) -> Response:
    await _container(request).products.delete(sku)
    return Response(status_code=204)


@router.post("/stores", response_model=StoreResponse, status_code=201, tags=["stores"])
async def create_store(body: StoreCreate, request: Request) -> dict[str, object]:
    store = await _container(request).stores.create(
        store_id=body.id,
        name=body.name,
        region=body.region,
    )
    return store_out(store)


@router.get("/stores", response_model=StorePage, tags=["stores"])
async def list_stores(request: Request) -> dict[str, object]:
    stores = await _container(request).stores.list()
    return page([store_out(item) for item in stores], limit=len(stores), offset=0)


@router.get("/stores/{store_id}", response_model=StoreResponse, tags=["stores"])
async def get_store(store_id: str, request: Request) -> dict[str, object]:
    return store_out(await _container(request).stores.get(store_id))


@router.put(
    "/inventory/{sku}/{store_id}",
    response_model=InventoryUpdateResponse,
    tags=["inventory"],
    responses={
        201: {"model": InventoryUpdateResponse},
        409: {"model": ErrorResponse},
    },
)
async def upsert_inventory(
    sku: str,
    store_id: str,
    body: InventoryUpsert,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key"),
) -> JSONResponse:
    key = idempotency_key.strip()
    if _IDEMPOTENCY_KEY.fullmatch(key) is None:
        raise InvalidRequestError(
            "Idempotency-Key must be 8-200 characters of letters, digits, or . _ : -"
        )
    result = await _container(request).inventory.upsert(
        UpsertInventory(
            sku=sku,
            store_id=store_id,
            quantity=body.quantity,
            expected_version=body.expected_version,
            threshold_override=body.threshold_override,
            threshold_override_provided="threshold_override" in body.model_fields_set,
            idempotency_key=key,
            request_hash=_inventory_hash(sku, store_id, body, body.model_fields_set),
            correlation_id=_correlation(request),
        )
    )
    headers = {"Idempotency-Replayed": "true"} if result.replayed else {}
    return JSONResponse(status_code=result.status_code, content=result.body, headers=headers)


@router.get(
    "/inventory/{sku}/{store_id}",
    response_model=InventoryResponse,
    tags=["inventory"],
)
async def get_inventory(sku: str, store_id: str, request: Request) -> dict[str, object]:
    return inventory_out(await _container(request).inventory.get(sku, store_id))


@router.get("/inventory", response_model=InventoryPage, tags=["inventory"])
async def list_inventory(
    request: Request,
    sku: str | None = None,
    store_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    rows = await _container(request).inventory.list(
        sku=sku,
        store_id=store_id,
        limit=limit,
        offset=offset,
    )
    return page([inventory_out(item) for item in rows], limit=limit, offset=offset)


@router.get(
    "/thresholds/{sku}/{store_id}",
    response_model=ThresholdResponse,
    tags=["inventory"],
)
async def get_threshold(sku: str, store_id: str, request: Request) -> dict[str, object]:
    container = _container(request)
    decision = await container.thresholds.get(sku, store_id)
    return {
        "sku": sku,
        "store_id": store_id,
        "effective_threshold": decision.value,
        "source": decision.source,
        "global_default": decision.global_default,
        "sku_override": decision.sku_override,
        "sku_store_override": decision.sku_store_override,
        "comparison": "quantity < threshold",
        "cooldown_seconds": container.settings.low_stock_cooldown_seconds,
    }


@router.post(
    "/listings/{sku}/stores/{store_id}/rewrite",
    response_model=ListingDraftResponse,
    status_code=201,
    tags=["listings"],
)
async def rewrite_listing(sku: str, store_id: str, request: Request) -> dict[str, object]:
    draft = await _container(request).listings.generate(
        GenerateListing(
            sku=sku,
            store_id=store_id,
            trigger_event_id=str(uuid4()),
            correlation_id=_correlation(request),
            reason="manual",
        )
    )
    return draft_out(draft)


@router.post(
    "/listings/drafts/{draft_id}/publish",
    response_model=StoreListingResponse,
    tags=["listings"],
    responses={409: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
async def publish_listing(
    draft_id: str,
    body: PublishListing,
    request: Request,
) -> dict[str, object]:
    listing = await _container(request).listings.publish(draft_id, body.expected_version)
    return store_listing_out(listing)


@router.get(
    "/listings/{sku}/stores/{store_id}",
    response_model=StoreListingResponse,
    tags=["listings"],
)
async def get_live_listing(sku: str, store_id: str, request: Request) -> dict[str, object]:
    listing = await _container(request).listings.get_live(sku, store_id)
    return store_listing_out(listing)


@router.get("/listings/drafts", response_model=ListingDraftPage, tags=["listings"])
async def list_listing_drafts(
    request: Request,
    sku: str | None = None,
    store_id: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    rows = await _container(request).listings.list(
        sku=sku,
        store_id=store_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return page([draft_out(item) for item in rows], limit=limit, offset=offset)


@router.get("/email-drafts", response_model=EmailDraftPage, tags=["email"])
async def list_email_drafts(
    request: Request,
    sku: str | None = None,
    store_id: str | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    rows = await _container(request).emails.list(
        sku=sku,
        store_id=store_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return page([email_out(item) for item in rows], limit=limit, offset=offset)


@router.post(
    "/email-suppressions",
    response_model=EmailSuppressionResponse,
    status_code=201,
    tags=["email"],
    responses={409: {"model": ErrorResponse}},
)
async def create_email_suppression(
    body: EmailSuppressionCreate, request: Request
) -> dict[str, object]:
    suppression = await _container(request).suppressions.create(
        customer_id=body.customer_id,
        reason=body.reason,
    )
    return suppression_out(suppression)


@router.get("/email-suppressions", response_model=EmailSuppressionPage, tags=["email"])
async def list_email_suppressions(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    rows = await _container(request).suppressions.list(limit=limit, offset=offset)
    return page([suppression_out(item) for item in rows], limit=limit, offset=offset)


@router.get("/email-handoffs", response_model=EmailHandoffPage, tags=["email"])
async def list_email_handoffs(
    request: Request,
    status: str | None = None,
    sku: str | None = None,
    store_id: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    rows = await _container(request).handoffs.list(
        status=status,
        sku=sku,
        store_id=store_id,
        limit=limit,
        offset=offset,
    )
    return page([handoff_out(item) for item in rows], limit=limit, offset=offset)


@router.get("/dead-letters", response_model=DeadLetterPage, tags=["ops"])
async def list_dead_letters(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    rows = await _container(request).dead_letters.list_with_redrives(limit=limit, offset=offset)
    return page([dead_letter_out(item) for item in rows], limit=limit, offset=offset)


@router.post(
    "/dead-letters/{dead_letter_id}/redrive",
    response_model=DeadLetterRedriveResponse,
    tags=["ops"],
    responses={
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
    },
)
async def redrive_dead_letter(dead_letter_id: str, request: Request) -> dict[str, object]:
    """Run only this letter's handler on its stored event. No body. Nothing is sent."""

    result = await _container(request).redrives.redrive(dead_letter_id)
    return redrive_out(result)
