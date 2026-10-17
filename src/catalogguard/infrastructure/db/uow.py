"""One database transaction per unit of work."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from catalogguard.infrastructure.db.repositories import (
    CooldownRepository,
    DeadLetterRedriveRepository,
    DeadLetterRepository,
    EmailDraftRepository,
    EmailHandoffRepository,
    EmailSuppressionRepository,
    IdempotencyRepository,
    InteractionRepository,
    InventoryChangeRepository,
    InventoryRepository,
    ListingDraftRepository,
    LlmUsageRepository,
    OutboxRepository,
    ProcessedEventRepository,
    ProductRepository,
    StoreListingRepository,
    StoreListingRevisionRepository,
    StoreRepository,
)


class SqlUnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.session: AsyncSession | None = None

    async def __aenter__(self) -> "SqlUnitOfWork":
        self.session = self._session_factory()
        session = self.session
        self.products = ProductRepository(session)
        self.stores = StoreRepository(session)
        self.inventory = InventoryRepository(session)
        self.inventory_changes = InventoryChangeRepository(session)
        self.idempotency = IdempotencyRepository(session)
        self.outbox = OutboxRepository(session)
        self.processed = ProcessedEventRepository(session)
        self.cooldowns = CooldownRepository(session)
        self.drafts = ListingDraftRepository(session)
        self.live_listings = StoreListingRepository(session)
        self.listing_revisions = StoreListingRevisionRepository(session)
        self.usage = LlmUsageRepository(session)
        self.emails = EmailDraftRepository(session)
        self.suppressions = EmailSuppressionRepository(session)
        self.handoffs = EmailHandoffRepository(session)
        self.interactions = InteractionRepository(session)
        self.dead_letters = DeadLetterRepository(session)
        self.redrives = DeadLetterRedriveRepository(session)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.session is None:
            return
        try:
            if exc_type is not None:
                await self.session.rollback()
        finally:
            await self.session.close()
            self.session = None

    async def commit(self) -> None:
        if self.session is None:
            raise RuntimeError("Unit of work is closed")
        await self.session.commit()

    async def rollback(self) -> None:
        if self.session is None:
            raise RuntimeError("Unit of work is closed")
        await self.session.rollback()


class SqlUnitOfWorkFactory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> SqlUnitOfWork:
        return SqlUnitOfWork(self._session_factory)
