"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from catalogguard import __version__
from catalogguard.application.listing_validation import MalformedLLMOutputError
from catalogguard.domain.errors import DomainError, OptimisticConflictError
from catalogguard.infrastructure.api.routes import ops_router, router
from catalogguard.infrastructure.config import Settings
from catalogguard.infrastructure.container import build_runtime
from catalogguard.infrastructure.db.session import init_db
from catalogguard.infrastructure.logging import configure_logging
from catalogguard.seed import seed

_DESCRIPTION = """
Product catalog and per-store inventory.

Every inventory change writes an InventoryChanged event. A downward crossing
of the effective low-stock threshold then writes LowInventoryDetected.
Handlers store a new listing draft and, unless the store is sold out, a
low-stock email draft. Live product SEO fields are never overwritten. Email
is not sent.
""".strip()


def create_app(settings: Settings | None = None, **kwargs: object) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    runtime = build_runtime(settings, **kwargs)  # type: ignore[arg-type]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Postgres schema comes from Alembic. create_all cannot add columns
        # to tables that already exist, so it runs only for SQLite.
        if app.state.settings.database_url.startswith("sqlite"):
            await init_db(app.state.engine)
        if app.state.settings.seed_on_startup:
            await seed(app.state.session_factory)
        await app.state.bus.drain()
        try:
            yield
        finally:
            await app.state.llm.aclose()
            await app.state.engine.dispose()

    app = FastAPI(
        title="CatalogGuard",
        version=__version__,
        description=_DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = runtime.settings
    app.state.clock = runtime.clock
    app.state.engine = runtime.engine
    app.state.session_factory = runtime.session_factory
    app.state.uow_factory = runtime.uow_factory
    app.state.metrics = runtime.metrics
    app.state.llm = runtime.llm
    app.state.bus = runtime.bus
    app.state.container = runtime.container

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        raw_correlation = request.headers.get("x-correlation-id")
        if raw_correlation is not None and len(raw_correlation) > 64:
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "code": "invalid_request",
                        "message": "X-Correlation-ID must be at most 64 characters",
                    }
                },
            )
        correlation_id = raw_correlation or str(uuid4())
        request.state.correlation_id = correlation_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers["X-Correlation-ID"] = correlation_id
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        body: dict[str, object] = {"code": exc.code, "message": str(exc)}
        if isinstance(exc, OptimisticConflictError):
            body["current_version"] = exc.current_version
        return JSONResponse(status_code=exc.http_status, content={"error": body})

    @app.exception_handler(MalformedLLMOutputError)
    async def malformed_handler(_request: Request, exc: MalformedLLMOutputError) -> JSONResponse:
        return JSONResponse(
            status_code=502,
            content={"error": {"code": "malformed_llm_output", "message": str(exc)}},
        )

    app.include_router(ops_router)
    app.include_router(router)
    return app


app = create_app()
