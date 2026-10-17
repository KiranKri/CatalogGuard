"""Wire ports to adapters. The API and the tests both call this."""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from catalogguard.application.catalog import ProductService, StoreService
from catalogguard.application.email_drafts import EmailDraftService
from catalogguard.application.email_handoff import EmailHandoffService, EmailSuppressionService
from catalogguard.application.handlers import (
    EmailDraftHandler,
    ListingRewriteHandler,
    ThresholdHandler,
)
from catalogguard.application.inventory import InventoryService
from catalogguard.application.listing import ListingService
from catalogguard.application.ports import Clock, LLMProvider, Metrics, UnitOfWorkFactory
from catalogguard.application.queries import DeadLetterService, OutboxSummaryService
from catalogguard.application.redrive import DeadLetterRedriveService
from catalogguard.application.thresholds import ThresholdService
from catalogguard.infrastructure.clock import SystemClock
from catalogguard.infrastructure.config import Settings
from catalogguard.infrastructure.db.session import create_db_engine, create_session_factory
from catalogguard.infrastructure.db.uow import SqlUnitOfWorkFactory
from catalogguard.infrastructure.events.bus import InProcessEventBus
from catalogguard.infrastructure.events.serialize import load_event
from catalogguard.infrastructure.llm.openai_compatible import OpenAICompatibleProvider
from catalogguard.infrastructure.llm.retrying import RetryingLLM
from catalogguard.infrastructure.llm.stub import StubLLM
from catalogguard.infrastructure.llm.templates import PromptLibrary
from catalogguard.infrastructure.metrics import PrometheusMetrics


@dataclass
class Container:
    settings: Settings
    clock: Clock
    products: ProductService
    stores: StoreService
    inventory: InventoryService
    thresholds: ThresholdService
    listings: ListingService
    emails: EmailDraftService
    suppressions: EmailSuppressionService
    handoffs: EmailHandoffService
    dead_letters: DeadLetterService
    redrives: DeadLetterRedriveService
    outbox: OutboxSummaryService


@dataclass
class Runtime:
    settings: Settings
    clock: Clock
    engine: AsyncEngine
    session_factory: object
    uow_factory: SqlUnitOfWorkFactory
    metrics: PrometheusMetrics
    llm: RetryingLLM
    bus: InProcessEventBus
    container: Container


def build_llm(settings: Settings) -> LLMProvider:
    provider = settings.llm_provider.lower()
    if provider == "stub":
        return StubLLM()
    if provider == "openai":
        api_key = settings.resolved_api_key()
        if not api_key:
            raise RuntimeError("LLM_PROVIDER=openai requires LLM_API_KEY or XAI_API_KEY")
        return OpenAICompatibleProvider(
            base_url=settings.llm_base_url,
            api_key=api_key,
            model=settings.llm_model,
            timeout=settings.llm_timeout_seconds,
        )
    raise RuntimeError(f"Unknown LLM_PROVIDER '{settings.llm_provider}'. Use stub or openai.")


def build_handoff_service(
    settings: Settings,
    uow_factory: UnitOfWorkFactory,
    *,
    clock: Clock,
    metrics: Metrics,
) -> EmailHandoffService:
    """Shared by the API container and `python -m catalogguard.mailer`."""

    return EmailHandoffService(
        uow_factory,
        metrics=metrics,
        clock=clock,
        batch_size=settings.email_handoff_batch_size,
        max_age_hours=settings.email_handoff_max_age_hours,
    )


def build_runtime(
    settings: Settings,
    *,
    clock: Clock | None = None,
    llm: LLMProvider | None = None,
) -> Runtime:
    if settings.handler_max_retries < 1 or settings.llm_max_retries < 1:
        raise RuntimeError("Retry counts must be at least 1")
    if settings.event_dispatch not in {"inline", "async"}:
        raise RuntimeError("EVENT_DISPATCH must be inline or async")
    if not 0 < settings.outbox_lease_renew_seconds < settings.outbox_claim_timeout_seconds:
        raise RuntimeError(
            "OUTBOX_LEASE_RENEW_SECONDS must be greater than 0 and less than "
            f"OUTBOX_CLAIM_TIMEOUT_SECONDS (got {settings.outbox_lease_renew_seconds} "
            f"and {settings.outbox_claim_timeout_seconds})"
        )
    prompts_path = settings.prompts_path()
    if not prompts_path.is_dir():
        raise RuntimeError(f"Prompts directory not found: {prompts_path}")

    clock = clock or SystemClock()
    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)
    uow_factory = SqlUnitOfWorkFactory(session_factory)
    metrics = PrometheusMetrics()
    library = PromptLibrary(prompts_path)
    listing_prompt = library.load("listing_rewrite", "v1")
    email_template = library.load_text("email_urgency", "v1")
    retrying = RetryingLLM(
        llm or build_llm(settings),
        attempts=settings.llm_max_retries,
        backoff_seconds=settings.retry_backoff_seconds,
        metrics=metrics,
    )
    bus = InProcessEventBus(
        uow_factory=uow_factory,
        metrics=metrics,
        clock=clock,
        max_retries=settings.handler_max_retries,
        backoff_seconds=settings.retry_backoff_seconds,
        mode=settings.event_dispatch,
        claim_timeout_seconds=settings.outbox_claim_timeout_seconds,
        lease_renew_seconds=settings.outbox_lease_renew_seconds,
    )
    products = ProductService(uow_factory, clock)
    stores = StoreService(uow_factory, clock)
    inventory = InventoryService(uow_factory, bus, clock)
    thresholds = ThresholdService(
        uow_factory,
        default_threshold=settings.default_low_stock_threshold,
    )
    listings = ListingService(
        uow_factory,
        retrying,
        listing_prompt,
        metrics,
        clock,
        daily_cap=settings.llm_daily_cap_per_sku,
    )
    emails = EmailDraftService(
        uow_factory,
        email_template,
        lookback_hours=settings.email_lookback_hours,
        metrics=metrics,
        clock=clock,
    )
    bus.subscribe(
        "InventoryChanged",
        ThresholdHandler(
            uow_factory,
            bus,
            default_threshold=settings.default_low_stock_threshold,
            cooldown_seconds=settings.low_stock_cooldown_seconds,
        ),
    )
    bus.subscribe("LowInventoryDetected", ListingRewriteHandler(listings))
    bus.subscribe("LowInventoryDetected", EmailDraftHandler(emails))
    container = Container(
        settings=settings,
        clock=clock,
        products=products,
        stores=stores,
        inventory=inventory,
        thresholds=thresholds,
        listings=listings,
        emails=emails,
        suppressions=EmailSuppressionService(uow_factory, clock),
        handoffs=build_handoff_service(settings, uow_factory, clock=clock, metrics=metrics),
        dead_letters=DeadLetterService(uow_factory),
        outbox=OutboxSummaryService(
            uow_factory,
            clock=clock,
            metrics=metrics,
            claim_timeout_seconds=settings.outbox_claim_timeout_seconds,
        ),
        redrives=DeadLetterRedriveService(
            uow_factory,
            handlers=bus,
            bus=bus,
            decode_event=load_event,
            metrics=metrics,
            clock=clock,
        ),
    )
    return Runtime(
        settings=settings,
        clock=clock,
        engine=engine,
        session_factory=session_factory,
        uow_factory=uow_factory,
        metrics=metrics,
        llm=retrying,
        bus=bus,
        container=container,
    )
