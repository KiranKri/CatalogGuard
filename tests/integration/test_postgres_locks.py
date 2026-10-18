"""Postgres row locks. Skipped unless TEST_DATABASE_URL points at Postgres."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tests.support import create_product, create_store

from catalogguard.domain.events import InventoryChanged
from catalogguard.domain.models import DeadLetter
from catalogguard.infrastructure.db.orm import OutboxORM
from catalogguard.infrastructure.db.repositories import OutboxRepository
from catalogguard.infrastructure.events.serialize import dump_event

pytestmark = pytest.mark.postgres_only

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _event() -> InventoryChanged:
    return InventoryChanged(
        event_id=str(uuid4()),
        event_type="InventoryChanged",
        occurred_at=NOW,
        correlation_id="pg-lock",
        sku="BKPK-28L-OLV",
        store_id="nyc",
        previous_quantity=10,
        new_quantity=2,
        inventory_version=2,
    )


async def _insert_outbox(app, count: int) -> list[str]:
    ids = []
    async with app.state.uow_factory() as uow:
        for _ in range(count):
            event = _event()
            ids.append(event.event_id)
            await uow.outbox.add(event)
        await uow.commit()
    return ids


async def _claim(engine, token: str) -> list[int]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        rows = await OutboxRepository(session).claim(
            limit=10,
            now=NOW,
            reclaim_before=NOW - timedelta(seconds=300),
            token=token,
        )
        await session.commit()
        return [row.id for row in rows]


async def test_two_claims_split_unlocked_rows(harness) -> None:
    _client, app = await harness()
    await _insert_outbox(app, 4)
    url = app.state.settings.database_url
    engine_a = create_async_engine(url)
    engine_b = create_async_engine(url)
    try:
        first, second = await asyncio.gather(
            _claim(engine_a, "worker-a"), _claim(engine_b, "worker-b")
        )
    finally:
        await engine_a.dispose()
        await engine_b.dispose()
    assert set(first).isdisjoint(second)
    assert len(first) + len(second) == 4


async def test_claim_skips_a_row_locked_by_another_engine(harness) -> None:
    _client, app = await harness()
    await _insert_outbox(app, 3)
    url = app.state.settings.database_url
    engine_a = create_async_engine(url)
    engine_b = create_async_engine(url)
    try:
        async with engine_a.connect() as connection:
            transaction = await connection.begin()
            locked = (
                await connection.execute(text("SELECT id FROM event_outbox ORDER BY id LIMIT 1"))
            ).scalar_one()
            await connection.execute(
                text("SELECT id FROM event_outbox WHERE id = :row_id FOR UPDATE"),
                {"row_id": locked},
            )
            claimed = await asyncio.wait_for(_claim(engine_b, "worker-b"), timeout=2)
            await transaction.rollback()
    finally:
        await engine_a.dispose()
        await engine_b.dispose()
    assert locked not in claimed
    assert len(claimed) == 2


async def test_mark_published_requires_the_lease_token(harness) -> None:
    _client, app = await harness()
    await _insert_outbox(app, 1)
    url = app.state.settings.database_url
    engine = create_async_engine(url)
    try:
        [row_id] = await _claim(engine, "holder")
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await OutboxRepository(session).mark_published(row_id, when=NOW, claim_token="other")
            await session.commit()
        async with factory() as session:
            still = await session.get(OutboxORM, row_id)
            assert still.published_at is None
            assert still.claim_token == "holder"
            await OutboxRepository(session).mark_published(row_id, when=NOW, claim_token="holder")
            await session.commit()
        async with factory() as session:
            published = await session.get(OutboxORM, row_id)
            assert published.published_at is not None
    finally:
        await engine.dispose()


async def test_two_redrives_one_handler(harness) -> None:
    client_a, app_a = await harness()
    client_b, _app_b = await harness(share=app_a)
    await create_store(client_a)
    await create_product(client_a)
    event = _event()
    letter_id = str(uuid4())
    async with app_a.state.uow_factory() as uow:
        await uow.dead_letters.add(
            DeadLetter(
                id=letter_id,
                event_id=event.event_id,
                event_type=event.event_type,
                handler="threshold_evaluation",
                payload=dump_event(event),
                error="forced",
                attempts=1,
                created_at=app_a.state.clock.now(),
            )
        )
        await uow.commit()

    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow(_event) -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    for app in (app_a, _app_b):
        app.state.bus.handler_for("InventoryChanged", "threshold_evaluation").handle = slow

    async def second():
        await started.wait()
        return await client_b.post(f"/api/v1/dead-letters/{letter_id}/redrive")

    first_task = asyncio.create_task(client_a.post(f"/api/v1/dead-letters/{letter_id}/redrive"))
    second_task = asyncio.create_task(second())
    await started.wait()
    blocked = await second_task
    release.set()
    first = await first_task

    assert calls == 1
    statuses = sorted([first.status_code, blocked.status_code])
    assert statuses == [200, 409]
    assert blocked.json()["error"]["code"] == "conflict"
    assert "already in progress" in blocked.json()["error"]["message"]
