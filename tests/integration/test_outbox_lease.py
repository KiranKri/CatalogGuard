"""Outbox lease renewal, the one-shot drain worker, and the backlog summary."""

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from tests.support import create_product, create_store, put_inventory

from catalogguard.application.ports import LEASE_IDLE, LEASE_LOST, LEASE_RENEWED
from catalogguard.domain.events import InventoryChanged
from catalogguard.drain import DrainResult, main, run_once
from catalogguard.infrastructure.db.orm import OutboxORM
from catalogguard.infrastructure.events.bus import InProcessEventBus, _Lease
from catalogguard.infrastructure.metrics import REGISTRY, PrometheusMetrics

SKU = "BKPK-28L-OLV"


def _metric(name: str) -> float:
    return REGISTRY.get_sample_value(name) or 0.0


def _lost() -> float:
    return _metric("catalogguard_outbox_lease_lost_total")


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


async def _add_event(app) -> InventoryChanged:
    event = InventoryChanged(
        event_id=str(uuid4()),
        event_type="InventoryChanged",
        occurred_at=app.state.clock.now(),
        correlation_id="corr-lease",
        sku=SKU,
        store_id="nyc",
        previous_quantity=10,
        new_quantity=2,
        inventory_version=2,
    )
    async with app.state.uow_factory() as uow:
        await uow.outbox.add(event)
        await uow.commit()
    return event


async def _claim(app, now: datetime, token: str):
    async with app.state.uow_factory() as uow:
        rows = await uow.outbox.claim(
            limit=10, now=now, reclaim_before=now - timedelta(seconds=300), token=token
        )
        await uow.commit()
    return rows


async def _renew(app, token: str, now: datetime, ids: list[int]) -> str:
    async with app.state.uow_factory() as uow:
        state = await uow.outbox.renew(token=token, now=now, message_ids=ids)
        await uow.commit()
    return state


async def _mark_published(app, message_id: int, when: datetime, token: str) -> None:
    async with app.state.uow_factory() as uow:
        await uow.outbox.mark_published(message_id, when=when, claim_token=token)
        await uow.commit()


async def _row(app, event_id: str) -> OutboxORM:
    async with app.state.session_factory() as session:
        row = await session.scalar(select(OutboxORM).where(OutboxORM.event_id == event_id))
        session.expunge(row)
        return row


def _bus(app, metrics=None) -> InProcessEventBus:
    return InProcessEventBus(
        uow_factory=app.state.uow_factory,
        metrics=metrics or app.state.metrics,
        clock=app.state.clock,
        max_retries=1,
        backoff_seconds=0,
        mode="inline",
        claim_timeout_seconds=300,
        lease_renew_seconds=60,
    )


class _Blocking:
    """Handler that stays in flight until the test releases it."""

    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def handle(self, event) -> None:
        del event
        self.calls += 1
        self.started.set()
        await self.release.wait()


async def _park_forever() -> None:
    await asyncio.Event().wait()


# Repository level


async def test_renew_moves_the_lease_and_is_idle_after_publish(harness) -> None:
    _client, app = await harness()
    now = app.state.clock.now()
    event = await _add_event(app)
    [message] = await _claim(app, now, "worker-a")
    lost = _lost()

    assert await _renew(app, "worker-a", now + timedelta(seconds=200), [message.id]) == (
        LEASE_RENEWED
    )
    row = await _row(app, event.event_id)
    assert _utc(row.claimed_at) == now + timedelta(seconds=200)
    # Without the renewal this row would be reclaimable at +301.
    assert await _claim(app, now + timedelta(seconds=301), "worker-b") == []

    await _mark_published(app, message.id, now + timedelta(seconds=210), "worker-a")
    assert await _renew(app, "worker-a", now + timedelta(seconds=220), [message.id]) == (LEASE_IDLE)
    row = await _row(app, event.event_id)
    assert row.published_at is not None
    assert row.claimed_at is None
    assert _lost() == lost


async def test_stolen_lease_is_lost_and_the_old_token_cannot_publish(harness) -> None:
    _client, app = await harness()
    now = app.state.clock.now()
    event = await _add_event(app)
    [message] = await _claim(app, now, "worker-a")
    later = now + timedelta(seconds=301)
    assert len(await _claim(app, later, "worker-b")) == 1

    assert await _renew(app, "worker-a", later, [message.id]) == LEASE_LOST
    await _mark_published(app, message.id, later, "worker-a")

    row = await _row(app, event.event_id)
    assert row.published_at is None
    assert row.claim_token == "worker-b"
    assert _utc(row.claimed_at) == later


# Bus level


async def test_keeper_renews_while_a_handler_is_still_running(harness) -> None:
    _client, app = await harness()
    clock = app.state.clock
    bus = _bus(app)
    handler = _Blocking()
    bus.subscribe("InventoryChanged", handler)
    renewed = asyncio.Event()
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 1:
            clock.advance(timedelta(seconds=seconds))
            return
        renewed.set()
        await _park_forever()

    bus._sleep = fake_sleep
    start = clock.now()
    event = await _add_event(app)
    drain = asyncio.create_task(bus.drain())
    await asyncio.wait_for(handler.started.wait(), 5)
    await asyncio.wait_for(renewed.wait(), 5)

    row = await _row(app, event.event_id)
    assert _utc(row.claimed_at) == start + timedelta(seconds=60)
    clock.advance(timedelta(seconds=250))
    # An unrenewed lease from `start` would be reclaimable now.
    assert await _claim(app, clock.now(), "intruder") == []

    handler.release.set()
    await asyncio.wait_for(drain, 5)
    assert waits[0] == 60
    assert handler.calls == 1
    assert (await _row(app, event.event_id)).published_at is not None


async def test_lost_lease_stops_the_batch_without_publishing_or_dead_lettering(harness) -> None:
    client, app = await harness()
    clock = app.state.clock
    lost_seen = asyncio.Event()

    class SpyMetrics(PrometheusMetrics):
        def outbox_lease_lost(self) -> None:
            super().outbox_lease_lost()
            lost_seen.set()

    bus = _bus(app, metrics=SpyMetrics())
    handler = _Blocking()
    bus.subscribe("InventoryChanged", handler)

    async def steal_then_return(seconds: float) -> None:
        # Another worker claims the batch after the lease timeout.
        clock.advance(timedelta(seconds=301))
        assert len(await _claim(app, clock.now(), "intruder")) == 2

    bus._sleep = steal_then_return
    first = await _add_event(app)
    second = await _add_event(app)
    lost, drain_failures = _lost(), _metric("catalogguard_drain_failures_total")
    drain = asyncio.create_task(bus.drain())
    await asyncio.wait_for(handler.started.wait(), 5)
    await asyncio.wait_for(lost_seen.wait(), 5)
    handler.release.set()
    await asyncio.wait_for(drain, 5)

    assert handler.calls == 1
    assert _lost() == lost + 1
    assert _metric("catalogguard_drain_failures_total") == drain_failures
    for event in (first, second):
        row = await _row(app, event.event_id)
        assert row.published_at is None
        assert row.claim_token == "intruder"
    assert (await client.get("/api/v1/dead-letters")).json()["items"] == []


async def test_keeper_after_the_last_publish_is_idle_and_not_lost(harness) -> None:
    _client, app = await harness()
    now = app.state.clock.now()
    await _add_event(app)
    [message] = await _claim(app, now, "worker-a")
    await _mark_published(app, message.id, now, "worker-a")
    bus = _bus(app)

    async def no_wait(seconds: float) -> None:
        del seconds

    bus._sleep = no_wait
    lease = _Lease(token="worker-a", message_ids=[message.id])
    lost = _lost()

    await asyncio.wait_for(bus._keep_lease(lease), 5)

    assert lease.lost is False
    assert _lost() == lost


# Drain worker and dispatch modes


async def test_drain_worker_publishes_what_an_async_api_left_behind(harness) -> None:
    client, app = await harness(event_dispatch="async")
    await create_store(client)
    await create_product(client)

    async def no_drain() -> None:
        return None

    app.state.bus.drain = no_drain
    created = await put_inventory(client, quantity=10, version=0, key="drain-create-0001")
    assert created.status_code == 201, created.text
    dropped = await put_inventory(client, quantity=2, version=1, key="drain-drop-00001")
    assert dropped.status_code == 200, dropped.text
    drafts_url = f"/api/v1/listings/drafts?sku={SKU}&store_id=nyc"
    emails_url = f"/api/v1/email-drafts?sku={SKU}&store_id=nyc"
    assert (await client.get(drafts_url)).json()["items"] == []
    assert (await client.get("/api/v1/outbox/summary")).json()["unpublished"] == 2

    first = await run_once(app.state.settings, clock=app.state.clock)

    assert first == DrainResult(unpublished_before=2, unpublished_after=0)
    [draft] = (await client.get(drafts_url)).json()["items"]
    assert draft["status"] == "ready"
    [email] = (await client.get(emails_url)).json()["items"]
    assert email["status"] == "queued"

    second = await run_once(app.state.settings, clock=app.state.clock)
    assert second.unpublished_after == 0


async def test_inline_dispatch_still_has_the_draft_when_the_put_returns(harness) -> None:
    client, app = await harness()
    assert app.state.settings.event_dispatch == "inline"
    await create_store(client)
    await create_product(client)
    await put_inventory(client, quantity=10, version=0, key="inline-create-001")
    dropped = await put_inventory(client, quantity=2, version=1, key="inline-drop-0001")
    assert dropped.status_code == 200, dropped.text

    drafts = await client.get(f"/api/v1/listings/drafts?sku={SKU}&store_id=nyc")
    assert len(drafts.json()["items"]) == 1
    assert (await client.get("/api/v1/outbox/summary")).json()["unpublished"] == 0


def test_drain_command_on_a_fresh_database_prints_a_zero_summary(
    tmp_path, monkeypatch, capsys
) -> None:
    database = (tmp_path / "fresh.db").resolve()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    main()

    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary == {"unpublished_before": 0, "unpublished_after": 0}
    connection = sqlite3.connect(database)
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    connection.close()
    assert {"event_outbox", "store_listing_revisions"} <= tables


# Backlog summary and startup check


async def test_outbox_summary_counts_backlog_and_live_leases(harness) -> None:
    client, app = await harness()
    clock = app.state.clock

    async def summary() -> dict:
        response = await client.get("/api/v1/outbox/summary")
        assert response.status_code == 200, response.text
        return response.json()

    assert await summary() == {
        "unpublished": 0,
        "claimed": 0,
        "oldest_unpublished_age_seconds": None,
    }
    assert _metric("catalogguard_outbox_oldest_unpublished_seconds") == 0

    await _add_event(app)
    clock.advance(timedelta(seconds=10))
    assert await summary() == {
        "unpublished": 1,
        "claimed": 0,
        "oldest_unpublished_age_seconds": 10,
    }
    assert _metric("catalogguard_outbox_unpublished") == 1
    assert _metric("catalogguard_outbox_oldest_unpublished_seconds") == 10

    await _claim(app, clock.now(), "worker-a")
    assert (await summary())["claimed"] == 1

    clock.advance(timedelta(seconds=301, milliseconds=900))
    after = await summary()
    assert (after["unpublished"], after["claimed"]) == (1, 0)
    assert after["oldest_unpublished_age_seconds"] == 311


@pytest.mark.parametrize("renew_seconds", [0, 300, 301])
async def test_renew_interval_outside_the_claim_timeout_fails_at_startup(
    harness, renew_seconds: int
) -> None:
    with pytest.raises(RuntimeError, match="OUTBOX_LEASE_RENEW_SECONDS"):
        await harness(outbox_lease_renew_seconds=renew_seconds)


async def test_drain_worker_applies_the_same_startup_check(harness) -> None:
    _client, app = await harness()
    bad = app.state.settings.model_copy(update={"outbox_lease_renew_seconds": 300})
    with pytest.raises(RuntimeError, match="OUTBOX_LEASE_RENEW_SECONDS"):
        await run_once(bad, clock=app.state.clock)
