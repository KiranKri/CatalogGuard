"""App fixture. Each call gets its own database and an inline event bus.

SQLite uses one file per build(). When TEST_DATABASE_URL is a postgresql+asyncpg
URL, each build() creates a database on that server and drops it afterwards.
Pass share=an app to build a second app on that app's database.
"""

import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from tests.fakes import FakeClock

from catalogguard.infrastructure.api.app import create_app
from catalogguard.infrastructure.config import Settings
from catalogguard.infrastructure.db.session import init_db


def pytest_collection_modifyitems(config, items) -> None:
    del config
    on_postgres = bool(os.environ.get("TEST_DATABASE_URL"))
    skip_sqlite = pytest.mark.skip(reason="SQLite-only check; TEST_DATABASE_URL is set")
    skip_postgres = pytest.mark.skip(reason="Needs Postgres; TEST_DATABASE_URL is not set")
    for item in items:
        if "sqlite_only" in item.keywords and on_postgres:
            item.add_marker(skip_sqlite)
        if "postgres_only" in item.keywords and not on_postgres:
            item.add_marker(skip_postgres)


def _admin_url():
    return make_url(os.environ["TEST_DATABASE_URL"])


async def create_postgres_database(name: str) -> str:
    """Create ``name`` on the TEST_DATABASE_URL server. Returns the app URL."""

    admin = _admin_url()
    engine = create_async_engine(
        admin.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
    )
    async with engine.connect() as connection:
        await connection.execute(text(f'CREATE DATABASE "{name}"'))
    await engine.dispose()
    return admin.set(database=name).render_as_string(hide_password=False)


async def drop_postgres_database(name: str) -> None:
    admin = _admin_url()
    engine = create_async_engine(
        admin.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
    )
    async with engine.connect() as connection:
        await connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": name},
        )
        await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    await engine.dispose()


@pytest.fixture
async def harness(tmp_path: Path):
    opened: list[tuple[AsyncClient, object]] = []
    created: list[str] = []

    async def build(*, llm=None, clock=None, share=None, **overrides):
        values = {
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
        if share is not None:
            values["database_url"] = share.state.settings.database_url
        elif "database_url" not in overrides and os.environ.get("TEST_DATABASE_URL"):
            name = f"cg_{uuid.uuid4().hex}"
            values["database_url"] = await create_postgres_database(name)
            created.append(name)
        elif "database_url" not in overrides:
            db_path = (tmp_path / f"{len(opened)}.db").resolve().as_posix()
            values["database_url"] = f"sqlite+aiosqlite:///{db_path}"
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
    for name in created:
        await drop_postgres_database(name)
