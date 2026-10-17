"""Crossings dropped inside the cooldown are stored and can be listed."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from tests.support import create_product, create_store, put_inventory

from catalogguard.domain.errors import ConflictError
from catalogguard.infrastructure.db.orm import ProcessedEventORM, SuppressedCrossingORM
from catalogguard.infrastructure.events.serialize import load_event

SKU = "BKPK-28L-OLV"
MUG = "MUG-CER-12"
WINDOW = timedelta(hours=24)
FIELDS = {
    "id",
    "sku",
    "store_id",
    "inventory_event_id",
    "occurred_at",
    "previous_quantity",
    "new_quantity",
    "threshold",
    "threshold_source",
    "cooldown_until",
    "reason",
}


async def _put(client, sku: str, quantity: int, version: int, key: str, **kwargs) -> None:
    response = await put_inventory(
        client, sku=sku, quantity=quantity, version=version, key=key, **kwargs
    )
    assert response.status_code in {200, 201}, response.text


async def _suppressed(client, query: str = "?store_id=nyc") -> list[dict]:
    response = await client.get(f"/api/v1/suppressed-crossings{query}")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    for item in items:
        assert set(item) == FIELDS
    return items


async def _drafts(client, sku: str = SKU) -> list[dict]:
    url = f"/api/v1/listings/drafts?sku={sku}&store_id=nyc"
    return (await client.get(url)).json()["items"]


async def _emails(client, sku: str = SKU) -> list[dict]:
    url = f"/api/v1/email-drafts?sku={sku}&store_id=nyc"
    return (await client.get(url)).json()["items"]


async def _newest_change(client, sku: str = SKU) -> dict:
    url = f"/api/v1/inventory/{sku}/nyc/changes?limit=1"
    return (await client.get(url)).json()["items"][0]


async def _last_triggered_at(app, sku: str = SKU) -> datetime:
    async with app.state.uow_factory() as uow:
        cooldown = await uow.cooldowns.get(sku, "nyc")
    assert cooldown is not None
    return cooldown.last_triggered_at


async def _row_count(app) -> int:
    async with app.state.session_factory() as session:
        return await session.scalar(select(func.count()).select_from(SuppressedCrossingORM))


async def _crossed_then_suppressed(harness):
    """10 → 4 fires. Within the next hour, a restock to 10 and a drop to 1 are suppressed."""

    client, app = await harness()
    clock = app.state.clock
    await create_store(client)
    await create_product(client)
    await _put(client, SKU, 10, 0, "supp-create-0001")
    await _put(client, SKU, 4, 1, "supp-cross-00001")
    first = await _newest_change(client)
    assert len(await _drafts(client)) == 1
    assert await _suppressed(client) == []

    # Distinct instants, so "newest change" does not fall back to the id tie-break.
    clock.advance(timedelta(minutes=30))
    await _put(client, SKU, 10, 2, "supp-restock-001")
    clock.advance(timedelta(minutes=30))
    await _put(client, SKU, 1, 3, "supp-drop-000001")
    return client, app, datetime.fromisoformat(first["occurred_at"])


async def test_a_crossing_inside_the_cooldown_is_stored_and_does_not_fire(harness) -> None:
    client, app, first_at = await _crossed_then_suppressed(harness)
    dropped = await _newest_change(client)

    [row] = await _suppressed(client)

    assert len(await _drafts(client)) == 1
    assert len(await _emails(client)) == 1
    assert row["sku"] == SKU
    assert row["store_id"] == "nyc"
    assert row["inventory_event_id"] == dropped["event_id"]
    assert row["occurred_at"] == dropped["occurred_at"]
    assert (row["previous_quantity"], row["new_quantity"]) == (10, 1)
    assert (row["threshold"], row["threshold_source"]) == (5, "global")
    assert row["reason"] == "cooldown"
    assert datetime.fromisoformat(row["cooldown_until"]) == first_at + WINDOW
    assert await _last_triggered_at(app) == first_at


async def test_a_crossing_at_the_end_of_the_window_fires_and_is_not_stored(harness) -> None:
    client, app, first_at = await _crossed_then_suppressed(harness)
    clock = app.state.clock
    clock.advance(timedelta(hours=1))
    await _put(client, SKU, 10, 4, "edge-restock-001")
    clock.advance(first_at + WINDOW - clock.now())

    await _put(client, SKU, 3, 5, "edge-cross-00001")

    assert len(await _drafts(client)) == 2
    assert len(await _suppressed(client)) == 1
    assert await _last_triggered_at(app) == first_at + WINDOW


async def test_a_move_that_stays_low_inside_the_window_is_not_stored(harness) -> None:
    client, app = await harness()
    await create_store(client)
    await create_product(client)
    await _put(client, SKU, 10, 0, "still-create-001")
    await _put(client, SKU, 4, 1, "still-cross-0001")
    app.state.clock.advance(timedelta(hours=1))

    await _put(client, SKU, 2, 2, "still-lower-0001")

    assert len(await _drafts(client)) == 1
    assert await _suppressed(client) == []
    assert await _row_count(app) == 0


async def test_a_first_insert_below_the_threshold_is_not_stored(harness) -> None:
    client, app = await harness()
    await create_store(client)
    await create_product(client)

    await _put(client, SKU, 2, 0, "first-low-00001")

    assert await _drafts(client) == []
    assert await _row_count(app) == 0


async def test_a_redelivered_event_is_not_stored_twice(harness) -> None:
    client, app, first_at = await _crossed_then_suppressed(harness)
    [row] = await _suppressed(client)
    handler = app.state.bus.handler_for("InventoryChanged", "threshold_evaluation")
    async with app.state.uow_factory() as uow:
        event = load_event(await uow.outbox.payload_by_event_id(row["inventory_event_id"]))

    await handler.handle(event)
    assert await _suppressed(client) == [row]

    # Without the processed marker, the unique inventory_event_id is the backstop.
    async with app.state.session_factory() as session:
        await session.execute(
            delete(ProcessedEventORM).where(
                ProcessedEventORM.event_id == event.event_id,
                ProcessedEventORM.handler == "threshold_evaluation",
            )
        )
        await session.commit()
    with pytest.raises(ConflictError):
        await handler.handle(event)
    assert await _suppressed(client) == [row]
    assert await _last_triggered_at(app) == first_at
    assert len(await _drafts(client)) == 1


async def test_listing_filters_by_store_and_sku_newest_first(harness) -> None:
    client, app = await harness()
    clock = app.state.clock
    missing = await client.get("/api/v1/suppressed-crossings?store_id=zzz")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert (await client.get("/api/v1/suppressed-crossings")).status_code == 422

    await create_store(client)
    await create_store(client, "chi", "Lakeshore")
    await create_product(client)
    await create_product(client, sku=MUG, name="Kiln Mug 12oz")
    assert await _suppressed(client) == []

    for index, sku in enumerate([SKU, MUG]):
        await _put(client, sku, 10, 0, f"list-{index}-create")
        await _put(client, sku, 4, 1, f"list-{index}-cross0")
    for index, sku in enumerate([SKU, MUG]):
        clock.advance(timedelta(minutes=10))
        await _put(client, sku, 10, 2, f"list-{index}-restck")
        await _put(client, sku, 1, 3, f"list-{index}-drop00")

    rows = await _suppressed(client)
    assert [row["sku"] for row in rows] == [MUG, SKU]
    assert rows[0]["occurred_at"] > rows[1]["occurred_at"]
    assert await _suppressed(client, f"?store_id=nyc&sku={SKU}") == [rows[1]]
    assert await _suppressed(client, "?store_id=nyc&limit=1&offset=1") == [rows[1]]
    assert await _suppressed(client, "?store_id=nyc&sku=NOPE-NOT-REAL") == []
    assert await _suppressed(client, "?store_id=chi") == []
    assert (
        await client.get("/api/v1/suppressed-crossings?store_id=nyc&limit=201")
    ).status_code == 422
