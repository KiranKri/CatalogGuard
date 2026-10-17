"""Ports. Infrastructure implements these. Application code depends on them."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from catalogguard.domain.events import DomainEvent
from catalogguard.domain.models import (
    CooldownState,
    DeadLetter,
    DeadLetterRedrive,
    EmailDraft,
    EmailHandoff,
    EmailSuppression,
    IdempotencyRecord,
    InventoryLevel,
    ListingDraft,
    Product,
    RecentInteraction,
    Store,
    StoreListing,
    StoreListingRevision,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class Metrics(Protocol):
    def event_emitted(self, event_type: str) -> None: ...

    def rewrite(self, result: str) -> None: ...

    def email(self, result: str) -> None: ...

    def handoff(self, result: str) -> None: ...

    def redrive(self, result: str) -> None: ...

    def dead_letter(self, handler: str) -> None: ...

    def drain_failure(self) -> None: ...

    def outbox_lease_lost(self) -> None: ...

    def outbox_backlog(self, unpublished: int, oldest_age_seconds: int | None) -> None: ...

    def handler_retry(self, handler: str) -> None: ...

    def observe_llm(self, seconds: float) -> None: ...


class LLMResponse(Protocol):
    content: str
    model: str


class LLMProvider(Protocol):
    async def complete(self, *, system: str, user: str) -> LLMResponse: ...

    async def aclose(self) -> None: ...


class ProductRepository(Protocol):
    async def get(self, sku: str) -> Product | None: ...

    async def add(self, product: Product) -> None: ...

    async def list(self, *, limit: int, offset: int) -> list[Product]: ...

    async def update(self, product: Product) -> None: ...

    async def delete(self, sku: str) -> None: ...


class StoreRepository(Protocol):
    async def get(self, store_id: str) -> Store | None: ...

    async def add(self, store: Store) -> None: ...

    async def list(self) -> list[Store]: ...


class InventoryRepository(Protocol):
    async def get(self, sku: str, store_id: str) -> InventoryLevel | None: ...

    async def add(self, level: InventoryLevel) -> None: ...

    async def compare_and_set(self, *, expected_version: int, level: InventoryLevel) -> bool: ...

    async def list(
        self, *, sku: str | None, store_id: str | None, limit: int, offset: int
    ) -> list[InventoryLevel]: ...

    async def any_for_sku(self, sku: str) -> bool: ...


class IdempotencyRepository(Protocol):
    async def get(self, key: str) -> IdempotencyRecord | None: ...

    async def add(self, record: IdempotencyRecord) -> None: ...


@dataclass(frozen=True)
class OutboxMessage:
    id: int
    payload: dict[str, object]
    claim_token: str | None = None


LEASE_RENEWED = "renewed"
LEASE_IDLE = "idle"
LEASE_LOST = "lost"


@dataclass(frozen=True)
class OutboxStats:
    unpublished: int
    claimed: int
    oldest_created_at: datetime | None


class OutboxRepository(Protocol):
    async def add(self, event: DomainEvent) -> None: ...

    async def claim(
        self,
        *,
        limit: int,
        now: datetime,
        reclaim_before: datetime,
        token: str,
    ) -> list[OutboxMessage]: ...

    async def mark_published(
        self, message_id: int, *, when: datetime, claim_token: str
    ) -> None: ...

    async def renew(self, *, token: str, now: datetime, message_ids: list[int]) -> str:
        """LEASE_RENEWED, LEASE_IDLE, or LEASE_LOST. Lost wins over renewed."""
        ...

    async def stats(self, *, claimed_since: datetime) -> OutboxStats: ...


class ProcessedEventRepository(Protocol):
    async def seen(self, event_id: str, handler: str) -> bool: ...

    async def mark(self, event_id: str, handler: str, processed_at: datetime) -> None: ...


class CooldownRepository(Protocol):
    async def get(self, sku: str, store_id: str) -> CooldownState | None: ...

    async def upsert(self, state: CooldownState) -> None: ...


class ListingDraftRepository(Protocol):
    async def get(self, draft_id: str) -> ListingDraft | None: ...

    async def get_by_trigger(self, trigger_event_id: str) -> ListingDraft | None: ...

    async def next_version(self, sku: str, store_id: str) -> int: ...

    async def add(self, draft: ListingDraft) -> None: ...

    async def set_status(self, draft_id: str, status: str) -> None: ...

    async def list(
        self,
        *,
        sku: str | None,
        store_id: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[ListingDraft]: ...


class StoreListingRepository(Protocol):
    async def get(self, sku: str, store_id: str) -> StoreListing | None: ...

    async def add(self, listing: StoreListing) -> None: ...

    async def compare_and_set(self, *, expected_version: int, listing: StoreListing) -> bool: ...


class StoreListingRevisionRepository(Protocol):
    async def add(self, revision: StoreListingRevision) -> None: ...

    async def get(self, sku: str, store_id: str, version: int) -> StoreListingRevision | None: ...

    async def list(
        self, *, sku: str, store_id: str, limit: int, offset: int
    ) -> list[StoreListingRevision]: ...


class LlmUsageRepository(Protocol):
    async def try_consume(self, sku: str, usage_date: str, cap: int) -> bool: ...


class EmailDraftRepository(Protocol):
    async def get_by_dedupe(self, dedupe_key: str) -> EmailDraft | None: ...

    async def add(self, draft: EmailDraft) -> None: ...

    async def list(
        self,
        *,
        sku: str | None,
        store_id: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[EmailDraft]: ...


class EmailSuppressionRepository(Protocol):
    async def get(self, customer_id: str) -> EmailSuppression | None: ...

    async def add(self, suppression: EmailSuppression) -> None: ...

    async def matching(self, customer_ids: list[str]) -> set[str]: ...

    async def list(self, *, limit: int, offset: int) -> list[EmailSuppression]: ...


class EmailHandoffRepository(Protocol):
    async def pending_drafts(self, *, limit: int) -> list[EmailDraft]: ...

    async def add(self, handoff: EmailHandoff) -> None: ...

    async def list(
        self,
        *,
        status: str | None,
        sku: str | None,
        store_id: str | None,
        limit: int,
        offset: int,
    ) -> list[EmailHandoff]: ...


class InteractionRepository(Protocol):
    async def list_since(
        self, *, sku: str, store_id: str, since: datetime, until: datetime
    ) -> list[RecentInteraction]: ...


class DeadLetterRepository(Protocol):
    async def get(self, letter_id: str) -> DeadLetter | None: ...

    async def add(self, letter: DeadLetter) -> None: ...

    async def list(self, *, limit: int, offset: int) -> list[DeadLetter]: ...


class DeadLetterRedriveRepository(Protocol):
    async def add(self, redrive: DeadLetterRedrive) -> None: ...

    async def first_success(self, dead_letter_id: str) -> DeadLetterRedrive | None: ...

    async def for_letters(self, dead_letter_ids: list[str]) -> list[DeadLetterRedrive]: ...


class UnitOfWork(Protocol):
    products: ProductRepository
    stores: StoreRepository
    inventory: InventoryRepository
    idempotency: IdempotencyRepository
    outbox: OutboxRepository
    processed: ProcessedEventRepository
    cooldowns: CooldownRepository
    drafts: ListingDraftRepository
    live_listings: StoreListingRepository
    listing_revisions: StoreListingRevisionRepository
    usage: LlmUsageRepository
    emails: EmailDraftRepository
    suppressions: EmailSuppressionRepository
    handoffs: EmailHandoffRepository
    interactions: InteractionRepository
    dead_letters: DeadLetterRepository
    redrives: DeadLetterRedriveRepository

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...


class EventBus(Protocol):
    async def publish(self, event: DomainEvent, outbox: OutboxRepository) -> None: ...

    async def drain(self) -> None: ...


class EventHandler(Protocol):
    async def handle(self, event: DomainEvent) -> None: ...


class HandlerRegistry(Protocol):
    """Finds the one subscribed handler instance, so a redrive reuses its wiring."""

    def handler_for(self, event_type: str, name: str) -> EventHandler | None: ...
