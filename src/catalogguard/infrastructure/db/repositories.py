"""SQL repositories. They return domain objects, not ORM rows."""

from datetime import UTC, datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from catalogguard.application.ports import (
    LEASE_IDLE,
    LEASE_LOST,
    LEASE_RENEWED,
    LatestDraft,
    OutboxMessage,
    OutboxStats,
)
from catalogguard.domain.errors import ConflictError, NotFoundError, OptimisticConflictError
from catalogguard.domain.events import DomainEvent
from catalogguard.domain.models import (
    CooldownState,
    DeadLetter,
    DeadLetterRedrive,
    EmailDraft,
    EmailHandoff,
    EmailSuppression,
    IdempotencyRecord,
    InventoryChange,
    InventoryLevel,
    ListingDraft,
    Product,
    RecentInteraction,
    Store,
    StoreListing,
    StoreListingRevision,
    SuppressedCrossing,
)
from catalogguard.infrastructure.db.orm import (
    CooldownORM,
    DeadLetterORM,
    DeadLetterRedriveClaimORM,
    DeadLetterRedriveORM,
    EmailDraftORM,
    EmailHandoffORM,
    EmailSuppressionORM,
    IdempotencyORM,
    InventoryChangeORM,
    InventoryLevelORM,
    ListingDraftORM,
    LlmUsageORM,
    OutboxORM,
    ProcessedEventORM,
    ProductORM,
    RecentInteractionORM,
    StoreListingORM,
    StoreListingRevisionORM,
    StoreORM,
    SuppressedCrossingORM,
)
from catalogguard.infrastructure.events.serialize import dump_event


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _copy_dict(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _copy_list(value: object) -> list:
    if isinstance(value, list):
        return list(value)
    return []


# Keeps one IN (...) list well under SQLite's bound-parameter limit.
_IN_CHUNK = 500


async def _latest_by_sku(
    session: AsyncSession, model: type, store_id: str, skus: list[str]
) -> dict[str, LatestDraft]:
    """Newest draft per SKU at one store (created_at, then id). Id and status only."""

    found: dict[str, LatestDraft] = {}
    wanted = sorted(set(skus))
    for start in range(0, len(wanted), _IN_CHUNK):
        chunk = wanted[start : start + _IN_CHUNK]
        rank = (
            func.row_number()
            .over(partition_by=model.sku, order_by=(model.created_at.desc(), model.id.desc()))
            .label("rank")
        )
        ranked = (
            select(model.sku, model.id, model.status, rank)
            .where(model.store_id == store_id, model.sku.in_(chunk))
            .subquery()
        )
        rows = await session.execute(
            select(ranked.c.sku, ranked.c.id, ranked.c.status).where(ranked.c.rank == 1)
        )
        for sku, draft_id, status in rows:
            found[sku] = LatestDraft(id=draft_id, status=status)
    return found


async def _savepoint_add(session: AsyncSession, row: object, error: Exception) -> None:
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError as exc:
        raise error from exc


def _product(row: ProductORM) -> Product:
    return Product(
        sku=row.sku,
        name=row.name,
        brand=row.brand,
        category=row.category,
        attributes=_copy_dict(row.attributes),
        base_description=row.base_description,
        seo_title=row.seo_title,
        seo_short_description=row.seo_short_description,
        seo_long_description=row.seo_long_description,
        meta_title=row.meta_title,
        meta_description=row.meta_description,
        threshold_override=row.threshold_override,
        created_at=_as_utc(row.created_at),
        updated_at=_as_utc(row.updated_at),
    )


def _store(row: StoreORM) -> Store:
    return Store(
        id=row.id,
        name=row.name,
        region=row.region,
        created_at=_as_utc(row.created_at),
    )


def _inventory(row: InventoryLevelORM) -> InventoryLevel:
    return InventoryLevel(
        sku=row.sku,
        store_id=row.store_id,
        quantity=row.quantity,
        threshold_override=row.threshold_override,
        updated_at=_as_utc(row.updated_at),
        version=row.version,
    )


def _draft(row: ListingDraftORM) -> ListingDraft:
    return ListingDraft(
        id=row.id,
        sku=row.sku,
        store_id=row.store_id,
        version=row.version,
        prompt_version=row.prompt_version,
        model=row.model,
        status=row.status,
        reason=row.reason,
        title=row.title,
        short_description=row.short_description,
        long_description=row.long_description,
        meta_title=row.meta_title,
        meta_description=row.meta_description,
        cited_attributes=[
            item for item in _copy_list(row.cited_attributes) if isinstance(item, str)
        ],
        violations=[item for item in _copy_list(row.violations) if isinstance(item, str)],
        raw_output=row.raw_output,
        trigger_event_id=row.trigger_event_id,
        correlation_id=row.correlation_id,
        created_at=_as_utc(row.created_at),
    )


def _email(row: EmailDraftORM) -> EmailDraft:
    return EmailDraft(
        id=row.id,
        sku=row.sku,
        store_id=row.store_id,
        status=row.status,
        payload=_copy_dict(row.payload),
        dedupe_key=row.dedupe_key,
        trigger_event_id=row.trigger_event_id,
        correlation_id=row.correlation_id,
        created_at=_as_utc(row.created_at),
    )


def _interaction(row: RecentInteractionORM) -> RecentInteraction:
    return RecentInteraction(
        id=row.id,
        customer_id=row.customer_id,
        sku=row.sku,
        store_id=row.store_id,
        interaction_type=row.interaction_type,
        timestamp=_as_utc(row.timestamp),
    )


class ProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, sku: str) -> Product | None:
        row = await self._session.get(ProductORM, sku)
        return None if row is None else _product(row)

    async def add(self, product: Product) -> None:
        await _savepoint_add(
            self._session,
            ProductORM(
                sku=product.sku,
                name=product.name,
                brand=product.brand,
                category=product.category,
                attributes=dict(product.attributes),
                base_description=product.base_description,
                seo_title=product.seo_title,
                seo_short_description=product.seo_short_description,
                seo_long_description=product.seo_long_description,
                meta_title=product.meta_title,
                meta_description=product.meta_description,
                threshold_override=product.threshold_override,
                created_at=product.created_at,
                updated_at=product.updated_at,
            ),
            ConflictError(f"Product {product.sku} already exists"),
        )

    async def threshold_overrides(self, skus: list[str]) -> dict[str, int | None]:
        wanted = sorted(set(skus))
        found: dict[str, int | None] = {}
        for start in range(0, len(wanted), _IN_CHUNK):
            chunk = wanted[start : start + _IN_CHUNK]
            stmt = select(ProductORM.sku, ProductORM.threshold_override).where(
                ProductORM.sku.in_(chunk)
            )
            for sku, override in await self._session.execute(stmt):
                found[sku] = override
        return found

    async def list(self, *, limit: int, offset: int) -> list[Product]:
        stmt = select(ProductORM).order_by(ProductORM.sku).limit(limit).offset(offset)
        rows = (await self._session.scalars(stmt)).all()
        return [_product(row) for row in rows]

    async def update(self, product: Product) -> None:
        row = await self._session.get(ProductORM, product.sku)
        if row is None:
            raise NotFoundError(f"Product {product.sku} not found")
        row.name = product.name
        row.brand = product.brand
        row.category = product.category
        row.attributes = dict(product.attributes)
        row.base_description = product.base_description
        row.seo_title = product.seo_title
        row.seo_short_description = product.seo_short_description
        row.seo_long_description = product.seo_long_description
        row.meta_title = product.meta_title
        row.meta_description = product.meta_description
        row.threshold_override = product.threshold_override
        row.updated_at = product.updated_at

    async def delete(self, sku: str) -> None:
        row = await self._session.get(ProductORM, sku)
        if row is None:
            raise NotFoundError(f"Product {sku} not found")
        await self._session.delete(row)
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"Product {sku} is still referenced and cannot be deleted") from exc


class StoreRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, store_id: str) -> Store | None:
        row = await self._session.get(StoreORM, store_id)
        return None if row is None else _store(row)

    async def add(self, store: Store) -> None:
        await _savepoint_add(
            self._session,
            StoreORM(
                id=store.id,
                name=store.name,
                region=store.region,
                created_at=store.created_at,
            ),
            ConflictError(f"Store {store.id} already exists"),
        )

    async def list(self) -> list[Store]:
        stmt = select(StoreORM).order_by(StoreORM.id)
        rows = (await self._session.scalars(stmt)).all()
        return [_store(row) for row in rows]


class InventoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, sku: str, store_id: str) -> InventoryLevel | None:
        row = await self._session.get(InventoryLevelORM, (sku, store_id))
        return None if row is None else _inventory(row)

    async def add(self, level: InventoryLevel) -> None:
        await _savepoint_add(
            self._session,
            InventoryLevelORM(
                sku=level.sku,
                store_id=level.store_id,
                quantity=level.quantity,
                threshold_override=level.threshold_override,
                updated_at=level.updated_at,
                version=level.version,
            ),
            OptimisticConflictError("Inventory row already exists", current_version=1),
        )

    async def compare_and_set(self, *, expected_version: int, level: InventoryLevel) -> bool:
        stmt = (
            update(InventoryLevelORM)
            .where(
                InventoryLevelORM.sku == level.sku,
                InventoryLevelORM.store_id == level.store_id,
                InventoryLevelORM.version == expected_version,
            )
            .values(
                quantity=level.quantity,
                threshold_override=level.threshold_override,
                updated_at=level.updated_at,
                version=level.version,
            )
        )
        result = await self._session.execute(stmt)
        if result.rowcount == 1:
            return True
        if result.rowcount == 0:
            return False
        latest = await self.get(level.sku, level.store_id)
        return latest is not None and latest.version == level.version

    async def for_store(self, store_id: str) -> list[InventoryLevel]:
        stmt = (
            select(InventoryLevelORM)
            .where(InventoryLevelORM.store_id == store_id)
            .order_by(InventoryLevelORM.sku)
        )
        rows = (await self._session.scalars(stmt)).all()
        return [_inventory(row) for row in rows]

    async def list(
        self, *, sku: str | None, store_id: str | None, limit: int, offset: int
    ) -> list[InventoryLevel]:
        stmt = select(InventoryLevelORM)
        if sku is not None:
            stmt = stmt.where(InventoryLevelORM.sku == sku)
        if store_id is not None:
            stmt = stmt.where(InventoryLevelORM.store_id == store_id)
        stmt = stmt.order_by(InventoryLevelORM.sku, InventoryLevelORM.store_id)
        stmt = stmt.limit(limit).offset(offset)
        rows = (await self._session.scalars(stmt)).all()
        return [_inventory(row) for row in rows]

    async def any_for_sku(self, sku: str) -> bool:
        stmt = select(InventoryLevelORM.sku).where(InventoryLevelORM.sku == sku).limit(1)
        return (await self._session.scalar(stmt)) is not None


def _inventory_change(row: InventoryChangeORM) -> InventoryChange:
    return InventoryChange(
        id=row.id,
        sku=row.sku,
        store_id=row.store_id,
        previous_quantity=row.previous_quantity,
        new_quantity=row.new_quantity,
        previous_threshold_override=row.previous_threshold_override,
        new_threshold_override=row.new_threshold_override,
        inventory_version=row.inventory_version,
        event_id=row.event_id,
        occurred_at=_as_utc(row.occurred_at),
    )


class InventoryChangeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, change: InventoryChange) -> None:
        await _savepoint_add(
            self._session,
            InventoryChangeORM(
                id=change.id,
                sku=change.sku,
                store_id=change.store_id,
                previous_quantity=change.previous_quantity,
                new_quantity=change.new_quantity,
                previous_threshold_override=change.previous_threshold_override,
                new_threshold_override=change.new_threshold_override,
                inventory_version=change.inventory_version,
                event_id=change.event_id,
                occurred_at=change.occurred_at,
            ),
            ConflictError("An inventory change already exists for this event"),
        )

    async def get_by_event(self, event_id: str) -> InventoryChange | None:
        stmt = select(InventoryChangeORM).where(InventoryChangeORM.event_id == event_id)
        row = await self._session.scalar(stmt)
        return None if row is None else _inventory_change(row)

    async def list(
        self, *, sku: str, store_id: str, limit: int, offset: int
    ) -> list[InventoryChange]:
        stmt = (
            select(InventoryChangeORM)
            .where(InventoryChangeORM.sku == sku, InventoryChangeORM.store_id == store_id)
            .order_by(InventoryChangeORM.occurred_at.desc(), InventoryChangeORM.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.scalars(stmt)).all()
        return [_inventory_change(row) for row in rows]


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> IdempotencyRecord | None:
        row = await self._session.get(IdempotencyORM, key)
        if row is None:
            return None
        return IdempotencyRecord(
            key=row.key,
            request_hash=row.request_hash,
            response_code=row.response_code,
            response_body=_copy_dict(row.response_body),
            created_at=_as_utc(row.created_at),
        )

    async def add(self, record: IdempotencyRecord) -> None:
        await _savepoint_add(
            self._session,
            IdempotencyORM(
                key=record.key,
                request_hash=record.request_hash,
                response_code=record.response_code,
                response_body=dict(record.response_body),
                created_at=record.created_at,
            ),
            ConflictError("Idempotency key already exists"),
        )


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: DomainEvent) -> None:
        self._session.add(
            OutboxORM(
                event_id=event.event_id,
                event_type=event.event_type,
                payload=dump_event(event),
                created_at=event.occurred_at,
                published_at=None,
                claimed_at=None,
                claim_token=None,
            )
        )

    async def claim(
        self,
        *,
        limit: int,
        now: datetime,
        reclaim_before: datetime,
        token: str,
    ) -> list[OutboxMessage]:
        """Lease unpublished rows. Postgres uses SKIP LOCKED. A lease older than
        reclaim_before can be taken again after a crashed worker."""

        stmt = (
            select(OutboxORM)
            .where(
                OutboxORM.published_at.is_(None),
                or_(OutboxORM.claimed_at.is_(None), OutboxORM.claimed_at < reclaim_before),
            )
            .order_by(OutboxORM.id)
            .limit(limit)
        )
        bind = self._session.bind
        if bind is not None and bind.dialect.name == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        rows = (await self._session.scalars(stmt)).all()
        claimed: list[OutboxMessage] = []
        for row in rows:
            row.claimed_at = now
            row.claim_token = token
            claimed.append(
                OutboxMessage(id=row.id, payload=_copy_dict(row.payload), claim_token=token)
            )
        if claimed:
            await self._session.flush()
        return claimed

    async def mark_published(self, message_id: int, *, when: datetime, claim_token: str) -> None:
        stmt = (
            update(OutboxORM)
            .where(
                OutboxORM.id == message_id,
                OutboxORM.published_at.is_(None),
                OutboxORM.claim_token == claim_token,
            )
            .values(published_at=when, claimed_at=None, claim_token=None)
        )
        await self._session.execute(stmt)

    async def renew(self, *, token: str, now: datetime, message_ids: list[int]) -> str:
        """Refresh a live lease.

        lost:    a still-unpublished row from the batch has a null or different token.
                 Checked first, so a partly stolen batch is not renewed or worked on.
        renewed: rows still held under this token, now with claimed_at = now.
        idle:    nothing left under this token. The normal end of a batch.
        Published rows are never touched.
        """

        if message_ids:
            stolen = await self._session.scalar(
                select(func.count())
                .select_from(OutboxORM)
                .where(
                    OutboxORM.id.in_(message_ids),
                    OutboxORM.published_at.is_(None),
                    or_(OutboxORM.claim_token.is_(None), OutboxORM.claim_token != token),
                )
            )
            if stolen:
                return LEASE_LOST
        result = await self._session.execute(
            update(OutboxORM)
            .where(OutboxORM.published_at.is_(None), OutboxORM.claim_token == token)
            .values(claimed_at=now)
        )
        return LEASE_RENEWED if result.rowcount else LEASE_IDLE

    async def payload_by_event_id(self, event_id: str) -> dict[str, object] | None:
        stmt = select(OutboxORM.payload).where(OutboxORM.event_id == event_id)
        payload = await self._session.scalar(stmt)
        return None if payload is None else _copy_dict(payload)

    async def stats(self, *, claimed_since: datetime) -> OutboxStats:
        pending = OutboxORM.published_at.is_(None)
        unpublished = await self._session.scalar(
            select(func.count()).select_from(OutboxORM).where(pending)
        )
        claimed = await self._session.scalar(
            select(func.count())
            .select_from(OutboxORM)
            .where(
                pending,
                OutboxORM.claimed_at.is_not(None),
                OutboxORM.claimed_at >= claimed_since,
            )
        )
        oldest = await self._session.scalar(select(func.min(OutboxORM.created_at)).where(pending))
        return OutboxStats(
            unpublished=int(unpublished or 0),
            claimed=int(claimed or 0),
            oldest_created_at=None if oldest is None else _as_utc(oldest),
        )


class ProcessedEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def seen(self, event_id: str, handler: str) -> bool:
        row = await self._session.get(ProcessedEventORM, (event_id, handler))
        return row is not None

    async def mark(self, event_id: str, handler: str, processed_at: datetime) -> None:
        try:
            async with self._session.begin_nested():
                self._session.add(
                    ProcessedEventORM(
                        event_id=event_id,
                        handler=handler,
                        processed_at=processed_at,
                    )
                )
                await self._session.flush()
        except IntegrityError as exc:
            raise ConflictError(f"Event {event_id} was already processed by {handler}") from exc


class CooldownRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, sku: str, store_id: str) -> CooldownState | None:
        row = await self._session.get(CooldownORM, (sku, store_id))
        if row is None:
            return None
        return CooldownState(
            sku=row.sku,
            store_id=row.store_id,
            last_triggered_at=_as_utc(row.last_triggered_at),
            last_event_id=row.last_event_id,
        )

    async def upsert(self, state: CooldownState) -> None:
        row = await self._session.get(CooldownORM, (state.sku, state.store_id))
        if row is None:
            self._session.add(
                CooldownORM(
                    sku=state.sku,
                    store_id=state.store_id,
                    last_triggered_at=state.last_triggered_at,
                    last_event_id=state.last_event_id,
                )
            )
            return
        row.last_triggered_at = state.last_triggered_at
        row.last_event_id = state.last_event_id


def _suppressed_crossing(row: SuppressedCrossingORM) -> SuppressedCrossing:
    return SuppressedCrossing(
        id=row.id,
        sku=row.sku,
        store_id=row.store_id,
        inventory_event_id=row.inventory_event_id,
        occurred_at=_as_utc(row.occurred_at),
        previous_quantity=row.previous_quantity,
        new_quantity=row.new_quantity,
        threshold=row.threshold,
        threshold_source=row.threshold_source,
        cooldown_until=_as_utc(row.cooldown_until),
        reason=row.reason,
    )


class SuppressedCrossingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, crossing: SuppressedCrossing) -> None:
        await _savepoint_add(
            self._session,
            SuppressedCrossingORM(
                id=crossing.id,
                sku=crossing.sku,
                store_id=crossing.store_id,
                inventory_event_id=crossing.inventory_event_id,
                occurred_at=crossing.occurred_at,
                previous_quantity=crossing.previous_quantity,
                new_quantity=crossing.new_quantity,
                threshold=crossing.threshold,
                threshold_source=crossing.threshold_source,
                cooldown_until=crossing.cooldown_until,
                reason=crossing.reason,
            ),
            ConflictError("A suppressed crossing already exists for this inventory event"),
        )

    async def list(
        self, *, store_id: str, sku: str | None, limit: int, offset: int
    ) -> list[SuppressedCrossing]:
        stmt = select(SuppressedCrossingORM).where(SuppressedCrossingORM.store_id == store_id)
        if sku is not None:
            stmt = stmt.where(SuppressedCrossingORM.sku == sku)
        stmt = (
            stmt.order_by(SuppressedCrossingORM.occurred_at.desc(), SuppressedCrossingORM.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.scalars(stmt)).all()
        return [_suppressed_crossing(row) for row in rows]


class ListingDraftRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, draft_id: str) -> ListingDraft | None:
        row = await self._session.get(ListingDraftORM, draft_id)
        return None if row is None else _draft(row)

    async def get_by_trigger(self, trigger_event_id: str) -> ListingDraft | None:
        stmt = select(ListingDraftORM).where(ListingDraftORM.trigger_event_id == trigger_event_id)
        row = await self._session.scalar(stmt)
        return None if row is None else _draft(row)

    async def next_version(self, sku: str, store_id: str) -> int:
        stmt = select(func.max(ListingDraftORM.version)).where(
            ListingDraftORM.sku == sku,
            ListingDraftORM.store_id == store_id,
        )
        current = await self._session.scalar(stmt)
        return int(current or 0) + 1

    async def add(self, draft: ListingDraft) -> None:
        await _savepoint_add(
            self._session,
            ListingDraftORM(
                id=draft.id,
                sku=draft.sku,
                store_id=draft.store_id,
                version=draft.version,
                prompt_version=draft.prompt_version,
                model=draft.model,
                status=draft.status,
                reason=draft.reason,
                title=draft.title,
                short_description=draft.short_description,
                long_description=draft.long_description,
                meta_title=draft.meta_title,
                meta_description=draft.meta_description,
                cited_attributes=list(draft.cited_attributes),
                violations=list(draft.violations),
                raw_output=draft.raw_output,
                trigger_event_id=draft.trigger_event_id,
                correlation_id=draft.correlation_id,
                created_at=draft.created_at,
            ),
            ConflictError("Listing draft already exists for this event or version"),
        )

    async def set_status(self, draft_id: str, status: str) -> None:
        row = await self._session.get(ListingDraftORM, draft_id)
        if row is None:
            raise NotFoundError(f"Listing draft {draft_id} not found")
        row.status = status

    async def latest_for_store(self, store_id: str, skus: list[str]) -> dict[str, LatestDraft]:
        return await _latest_by_sku(self._session, ListingDraftORM, store_id, skus)

    async def list(
        self,
        *,
        sku: str | None,
        store_id: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[ListingDraft]:
        stmt = select(ListingDraftORM)
        if sku is not None:
            stmt = stmt.where(ListingDraftORM.sku == sku)
        if store_id is not None:
            stmt = stmt.where(ListingDraftORM.store_id == store_id)
        if status is not None:
            stmt = stmt.where(ListingDraftORM.status == status)
        stmt = stmt.order_by(ListingDraftORM.created_at.desc(), ListingDraftORM.version.desc())
        stmt = stmt.limit(limit).offset(offset)
        rows = (await self._session.scalars(stmt)).all()
        return [_draft(row) for row in rows]


def _store_listing(row: StoreListingORM) -> StoreListing:
    return StoreListing(
        sku=row.sku,
        store_id=row.store_id,
        version=row.version,
        title=row.title,
        short_description=row.short_description,
        long_description=row.long_description,
        meta_title=row.meta_title,
        meta_description=row.meta_description,
        published_draft_id=row.published_draft_id,
        published_at=_as_utc(row.published_at),
    )


class StoreListingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, sku: str, store_id: str) -> StoreListing | None:
        row = await self._session.get(StoreListingORM, (sku, store_id))
        return None if row is None else _store_listing(row)

    async def add(self, listing: StoreListing) -> None:
        await _savepoint_add(
            self._session,
            StoreListingORM(
                sku=listing.sku,
                store_id=listing.store_id,
                version=listing.version,
                title=listing.title,
                short_description=listing.short_description,
                long_description=listing.long_description,
                meta_title=listing.meta_title,
                meta_description=listing.meta_description,
                published_draft_id=listing.published_draft_id,
                published_at=listing.published_at,
            ),
            OptimisticConflictError("Store listing already exists", current_version=1),
        )

    async def compare_and_set(self, *, expected_version: int, listing: StoreListing) -> bool:
        stmt = (
            update(StoreListingORM)
            .where(
                StoreListingORM.sku == listing.sku,
                StoreListingORM.store_id == listing.store_id,
                StoreListingORM.version == expected_version,
            )
            .values(
                version=listing.version,
                title=listing.title,
                short_description=listing.short_description,
                long_description=listing.long_description,
                meta_title=listing.meta_title,
                meta_description=listing.meta_description,
                published_draft_id=listing.published_draft_id,
                published_at=listing.published_at,
            )
        )
        result = await self._session.execute(stmt)
        return result.rowcount == 1


def _listing_revision(row: StoreListingRevisionORM) -> StoreListingRevision:
    return StoreListingRevision(
        id=row.id,
        sku=row.sku,
        store_id=row.store_id,
        version=row.version,
        published_draft_id=row.published_draft_id,
        title=row.title,
        short_description=row.short_description,
        long_description=row.long_description,
        meta_title=row.meta_title,
        meta_description=row.meta_description,
        published_at=_as_utc(row.published_at),
    )


class StoreListingRevisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, revision: StoreListingRevision) -> None:
        await _savepoint_add(
            self._session,
            StoreListingRevisionORM(
                id=revision.id,
                sku=revision.sku,
                store_id=revision.store_id,
                version=revision.version,
                published_draft_id=revision.published_draft_id,
                title=revision.title,
                short_description=revision.short_description,
                long_description=revision.long_description,
                meta_title=revision.meta_title,
                meta_description=revision.meta_description,
                published_at=revision.published_at,
            ),
            ConflictError("A revision already exists for that live listing version"),
        )

    async def get(self, sku: str, store_id: str, version: int) -> StoreListingRevision | None:
        stmt = select(StoreListingRevisionORM).where(
            StoreListingRevisionORM.sku == sku,
            StoreListingRevisionORM.store_id == store_id,
            StoreListingRevisionORM.version == version,
        )
        row = await self._session.scalar(stmt)
        return None if row is None else _listing_revision(row)

    async def list(
        self, *, sku: str, store_id: str, limit: int, offset: int
    ) -> list[StoreListingRevision]:
        stmt = (
            select(StoreListingRevisionORM)
            .where(
                StoreListingRevisionORM.sku == sku,
                StoreListingRevisionORM.store_id == store_id,
            )
            .order_by(StoreListingRevisionORM.version.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.scalars(stmt)).all()
        return [_listing_revision(row) for row in rows]


class LlmUsageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def try_consume(self, sku: str, usage_date: str, cap: int) -> bool:
        bind = self._session.bind
        dialect = bind.dialect.name if bind is not None else "sqlite"
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        stmt = dialect_insert(LlmUsageORM).values(sku=sku, usage_date=usage_date, call_count=1)
        stmt = stmt.on_conflict_do_update(
            index_elements=["sku", "usage_date"],
            set_={"call_count": LlmUsageORM.call_count + 1},
            where=LlmUsageORM.call_count < cap,
        )
        result = await self._session.execute(stmt)
        return result.rowcount == 1


class EmailDraftRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_dedupe(self, dedupe_key: str) -> EmailDraft | None:
        stmt = select(EmailDraftORM).where(EmailDraftORM.dedupe_key == dedupe_key)
        row = await self._session.scalar(stmt)
        return None if row is None else _email(row)

    async def add(self, draft: EmailDraft) -> None:
        await _savepoint_add(
            self._session,
            EmailDraftORM(
                id=draft.id,
                sku=draft.sku,
                store_id=draft.store_id,
                status=draft.status,
                payload=dict(draft.payload),
                dedupe_key=draft.dedupe_key,
                trigger_event_id=draft.trigger_event_id,
                correlation_id=draft.correlation_id,
                created_at=draft.created_at,
            ),
            ConflictError("Email draft already exists for this event"),
        )

    async def latest_for_store(self, store_id: str, skus: list[str]) -> dict[str, LatestDraft]:
        # Id and status only: no payload, so no recipients or body are read.
        return await _latest_by_sku(self._session, EmailDraftORM, store_id, skus)

    async def list(
        self,
        *,
        sku: str | None,
        store_id: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[EmailDraft]:
        stmt = select(EmailDraftORM)
        if sku is not None:
            stmt = stmt.where(EmailDraftORM.sku == sku)
        if store_id is not None:
            stmt = stmt.where(EmailDraftORM.store_id == store_id)
        if status is not None:
            stmt = stmt.where(EmailDraftORM.status == status)
        stmt = stmt.order_by(EmailDraftORM.created_at.desc())
        stmt = stmt.limit(limit).offset(offset)
        rows = (await self._session.scalars(stmt)).all()
        return [_email(row) for row in rows]


def _suppression(row: EmailSuppressionORM) -> EmailSuppression:
    return EmailSuppression(
        customer_id=row.customer_id,
        reason=row.reason,
        created_at=_as_utc(row.created_at),
    )


def _handoff(row: EmailHandoffORM) -> EmailHandoff:
    return EmailHandoff(
        id=row.id,
        email_draft_id=row.email_draft_id,
        status=row.status,
        payload=_copy_dict(row.payload),
        suppressed_customer_ids=[
            item for item in _copy_list(row.suppressed_customer_ids) if isinstance(item, str)
        ],
        created_at=_as_utc(row.created_at),
    )


class EmailSuppressionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, customer_id: str) -> EmailSuppression | None:
        row = await self._session.get(EmailSuppressionORM, customer_id)
        return None if row is None else _suppression(row)

    async def add(self, suppression: EmailSuppression) -> None:
        await _savepoint_add(
            self._session,
            EmailSuppressionORM(
                customer_id=suppression.customer_id,
                reason=suppression.reason,
                created_at=suppression.created_at,
            ),
            ConflictError("That customer id is already suppressed"),
        )

    async def matching(self, customer_ids: list[str]) -> set[str]:
        wanted = sorted(set(customer_ids))
        found: set[str] = set()
        for start in range(0, len(wanted), _IN_CHUNK):
            chunk = wanted[start : start + _IN_CHUNK]
            stmt = select(EmailSuppressionORM.customer_id).where(
                EmailSuppressionORM.customer_id.in_(chunk)
            )
            found.update((await self._session.scalars(stmt)).all())
        return found

    async def list(self, *, limit: int, offset: int) -> list[EmailSuppression]:
        stmt = (
            select(EmailSuppressionORM)
            .order_by(EmailSuppressionORM.customer_id)
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.scalars(stmt)).all()
        return [_suppression(row) for row in rows]


class EmailHandoffRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def pending_drafts(self, *, limit: int) -> list[EmailDraft]:
        """Queued drafts with no handoff decision yet, oldest first."""

        stmt = (
            select(EmailDraftORM)
            .outerjoin(EmailHandoffORM, EmailHandoffORM.email_draft_id == EmailDraftORM.id)
            .where(EmailDraftORM.status == "queued", EmailHandoffORM.id.is_(None))
            .order_by(EmailDraftORM.created_at, EmailDraftORM.id)
            .limit(limit)
        )
        rows = (await self._session.scalars(stmt)).all()
        return [_email(row) for row in rows]

    async def add(self, handoff: EmailHandoff) -> None:
        """Insert only. A second decision for the same draft raises ConflictError."""

        await _savepoint_add(
            self._session,
            EmailHandoffORM(
                id=handoff.id,
                email_draft_id=handoff.email_draft_id,
                status=handoff.status,
                payload=dict(handoff.payload),
                suppressed_customer_ids=list(handoff.suppressed_customer_ids),
                created_at=handoff.created_at,
            ),
            ConflictError("A handoff decision already exists for this email draft"),
        )

    async def list(
        self,
        *,
        status: str | None,
        sku: str | None,
        store_id: str | None,
        limit: int,
        offset: int,
    ) -> list[EmailHandoff]:
        stmt = select(EmailHandoffORM)
        if sku is not None or store_id is not None:
            stmt = stmt.join(EmailDraftORM, EmailDraftORM.id == EmailHandoffORM.email_draft_id)
            if sku is not None:
                stmt = stmt.where(EmailDraftORM.sku == sku)
            if store_id is not None:
                stmt = stmt.where(EmailDraftORM.store_id == store_id)
        if status is not None:
            stmt = stmt.where(EmailHandoffORM.status == status)
        stmt = stmt.order_by(EmailHandoffORM.created_at.desc(), EmailHandoffORM.id)
        stmt = stmt.limit(limit).offset(offset)
        rows = (await self._session.scalars(stmt)).all()
        return [_handoff(row) for row in rows]


class InteractionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_since(
        self, *, sku: str, store_id: str, since: datetime, until: datetime
    ) -> list[RecentInteraction]:
        stmt = (
            select(RecentInteractionORM)
            .where(
                RecentInteractionORM.sku == sku,
                RecentInteractionORM.store_id == store_id,
                RecentInteractionORM.timestamp >= since,
                RecentInteractionORM.timestamp <= until,
            )
            .order_by(RecentInteractionORM.timestamp.desc())
        )
        rows = (await self._session.scalars(stmt)).all()
        return [_interaction(row) for row in rows]


def _dead_letter(row: DeadLetterORM) -> DeadLetter:
    return DeadLetter(
        id=row.id,
        event_id=row.event_id,
        event_type=row.event_type,
        handler=row.handler,
        payload=_copy_dict(row.payload),
        error=row.error,
        attempts=row.attempts,
        created_at=_as_utc(row.created_at),
    )


def _redrive(row: DeadLetterRedriveORM) -> DeadLetterRedrive:
    return DeadLetterRedrive(
        id=row.id,
        dead_letter_id=row.dead_letter_id,
        status=row.status,
        error=row.error,
        created_at=_as_utc(row.created_at),
    )


class DeadLetterRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, letter_id: str) -> DeadLetter | None:
        row = await self._session.get(DeadLetterORM, letter_id)
        return None if row is None else _dead_letter(row)

    async def add(self, letter: DeadLetter) -> None:
        self._session.add(
            DeadLetterORM(
                id=letter.id,
                event_id=letter.event_id,
                event_type=letter.event_type,
                handler=letter.handler,
                payload=dict(letter.payload),
                error=letter.error,
                attempts=letter.attempts,
                created_at=letter.created_at,
            )
        )

    async def list(self, *, limit: int, offset: int) -> list[DeadLetter]:
        stmt = (
            select(DeadLetterORM)
            .order_by(DeadLetterORM.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.scalars(stmt)).all()
        return [_dead_letter(row) for row in rows]


class DeadLetterRedriveRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, redrive: DeadLetterRedrive) -> None:
        self._session.add(
            DeadLetterRedriveORM(
                id=redrive.id,
                dead_letter_id=redrive.dead_letter_id,
                status=redrive.status,
                error=redrive.error,
                created_at=redrive.created_at,
            )
        )
        await self._session.flush()

    async def first_success(self, dead_letter_id: str) -> DeadLetterRedrive | None:
        stmt = (
            select(DeadLetterRedriveORM)
            .where(
                DeadLetterRedriveORM.dead_letter_id == dead_letter_id,
                DeadLetterRedriveORM.status == "succeeded",
            )
            .order_by(DeadLetterRedriveORM.created_at, DeadLetterRedriveORM.id)
            .limit(1)
        )
        row = await self._session.scalar(stmt)
        return None if row is None else _redrive(row)

    async def for_letters(self, dead_letter_ids: list[str]) -> list[DeadLetterRedrive]:
        """All attempts for these letters, oldest first: created_at, then id."""

        wanted = sorted(set(dead_letter_ids))
        found: list[DeadLetterRedrive] = []
        for start in range(0, len(wanted), _IN_CHUNK):
            chunk = wanted[start : start + _IN_CHUNK]
            stmt = select(DeadLetterRedriveORM).where(
                DeadLetterRedriveORM.dead_letter_id.in_(chunk)
            )
            found.extend(_redrive(row) for row in (await self._session.scalars(stmt)).all())
        return sorted(found, key=lambda item: (item.created_at, item.id))


class DeadLetterRedriveClaimRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, dead_letter_id: str, claimed_at: datetime) -> bool:
        """Insert a claim. False when the letter already has one."""

        try:
            async with self._session.begin_nested():
                self._session.add(
                    DeadLetterRedriveClaimORM(dead_letter_id=dead_letter_id, claimed_at=claimed_at)
                )
                await self._session.flush()
        except IntegrityError:
            return False
        return True

    async def claimed_at(self, dead_letter_id: str) -> datetime | None:
        stmt = select(DeadLetterRedriveClaimORM.claimed_at).where(
            DeadLetterRedriveClaimORM.dead_letter_id == dead_letter_id
        )
        value = await self._session.scalar(stmt)
        return None if value is None else _as_utc(value)

    async def take_over(self, dead_letter_id: str, *, expected: datetime, now: datetime) -> bool:
        stmt = (
            update(DeadLetterRedriveClaimORM)
            .where(
                DeadLetterRedriveClaimORM.dead_letter_id == dead_letter_id,
                DeadLetterRedriveClaimORM.claimed_at == expected,
            )
            .values(claimed_at=now)
        )
        result = await self._session.execute(stmt)
        return result.rowcount == 1

    async def release(self, dead_letter_id: str, claimed_at: datetime) -> None:
        """Delete the claim only while it is still the one taken at claimed_at."""

        stmt = delete(DeadLetterRedriveClaimORM).where(
            DeadLetterRedriveClaimORM.dead_letter_id == dead_letter_id,
            DeadLetterRedriveClaimORM.claimed_at == claimed_at,
        )
        await self._session.execute(stmt)
