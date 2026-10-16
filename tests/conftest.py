"""App fixture. Each call gets its own SQLite file and an inline event bus."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from tests.fakes import FakeClock

from catalogguard.infrastructure.api.app import create_app
from catalogguard.infrastructure.config import Settings
from catalogguard.infrastructure.db.session import init_db


@pytest.fixture
async def harness(tmp_path: Path):
    opened: list[tuple[AsyncClient, object]] = []

    async def build(*, llm=None, clock=None, **overrides):
        db_path = (tmp_path / f"{len(opened)}.db").resolve().as_posix()
        values = {
            "database_url": f"sqlite+aiosqlite:///{db_path}",
            "event_dispatch": "inline",
            "llm_provider": "stub",
            "llm_max_retries": 1,
            "handler_max_retries": 2,
            "retry_backoff_seconds": 0,
            "seed_on_startup": False,
            "default_low_stock_threshold": 5,
            "low_stock_cooldown_seconds": 86400,
            "email_lookback_hours": 168,
            "log_level": "WARNING",
        }
        values.update(overrides)
        settings = Settings(**values)
        chosen_clock = clock or FakeClock(datetime(2026, 6, 1, 12, 0, tzinfo=UTC))
        application = create_app(settings, clock=chosen_clock, llm=llm)
        await init_db(application.state.engine)
        client = AsyncClient(transport=ASGITransport(app=application), base_url="http://test")
        await client.__aenter__()
        opened.append((client, application))
        return client, application

    yield build

    for client, application in opened:
        await client.aclose()
        await application.state.llm.aclose()
        await application.state.engine.dispose()
