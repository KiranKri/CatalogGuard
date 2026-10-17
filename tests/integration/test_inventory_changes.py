"""Append-only inventory change history: what is written, and what is not."""

from datetime import UTC, timedelta

from sqlalchemy import select
from tests.support import create_product, create_store, put_inventory

from catalogguard.infrastructure.db.orm import OutboxORM
from catalogguard.seed import seed

SKU = "BKPK-28L-OLV"
CHANGES_URL = f"/api/v1/inventory/{SKU}/nyc/changes"


async def _changes(client, url: str = CHANGES_URL) -> list[dict]:
    response = await client.get(url)
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _outbox_events(app) -> dict[str, str]:
    """event_id -> created_at (the event's occurred_at) for every outbox row."""

    async with app.state.session_factory() as session:
        rows = (await session.scalars(select(OutboxORM))).all()
    return {
        row.event_id: (
            row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
        ).isoformat()
        for row in rows
    }


async def test_history_records_only_writes_that_change_the_stock_row(harness) -> None:
    client, app = await harness()
    clock = app.state.clock
    await create_store(client)
    await create_product(client)

    created = await put_inventory(client, quantity=10, version=0, key="hist-create-0001")
    assert created.status_code == 201, created.text
    [first] = await _changes(client)
    assert first["sku"] == SKU
    assert first["store_id"] == "nyc"
    assert (first["previous_quantity"], first["new_quantity"]) == (None, 10)
    assert (first["previous_threshold_override"], first["new_threshold_override"]) == (None, None)
    assert first["inventory_version"] == 1
    assert first["occurred_at"] == clock.now().isoformat()

    unchanged = await put_inventory(client, quantity=10, version=1, key="hist-same-000001")
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["version"] == 1
    assert len(await _changes(client)) == 1

    clock.advance(timedelta(minutes=1))
    dropped = await put_inventory(client, quantity=2, version=1, key="hist-drop-000001")
    assert dropped.status_code == 200, dropped.text
    newest, oldest = await _changes(client)
    assert oldest["id"] == first["id"]
    assert (newest["previous_quantity"], newest["new_quantity"]) == (10, 2)
    assert newest["inventory_version"] == 2

    replay = await put_inventory(client, quantity=2, version=1, key="hist-drop-000001")
    assert replay.headers["idempotency-replayed"] == "true"
    assert len(await _changes(client)) == 2

    clock.advance(timedelta(minutes=1))
    override = await put_inventory(
        client, quantity=2, version=2, key="hist-override-01", threshold_override=8
    )
    assert override.status_code == 200, override.text
    third = (await _changes(client))[0]
    assert (third["previous_quantity"], third["new_quantity"]) == (2, 2)
    assert (third["previous_threshold_override"], third["new_threshold_override"]) == (None, 8)
    stock = (await client.get(f"/api/v1/inventory/{SKU}/nyc")).json()
    assert third["inventory_version"] == stock["version"] == 3

    clock.advance(timedelta(minutes=1))
    cleared = await put_inventory(
        client, quantity=2, version=3, key="hist-clear-00001", threshold_override=None
    )
    assert cleared.status_code == 200, cleared.text
    fourth = (await _changes(client))[0]
    assert (fourth["previous_threshold_override"], fourth["new_threshold_override"]) == (8, None)
    assert fourth["inventory_version"] == 4

    stale = await put_inventory(client, quantity=1, version=1, key="hist-stale-00001")
    assert stale.status_code == 409
    missing_store = await put_inventory(
        client, store_id="zzz", quantity=1, version=0, key="hist-404-0000001"
    )
    assert missing_store.status_code == 404

    history = await _changes(client)
    assert [item["inventory_version"] for item in history] == [4, 3, 2, 1]
    events = await _outbox_events(app)
    for item in history:
        assert events[item["event_id"]] == item["occurred_at"]
    assert len({item["event_id"] for item in history}) == 4

    paged = await _changes(client, f"{CHANGES_URL}?limit=1&offset=1")
    assert [item["inventory_version"] for item in paged] == [3]


async def test_unknown_pair_is_404_and_an_unstocked_pair_is_empty(harness) -> None:
    client, _app = await harness()
    await create_store(client)

    unknown_sku = await client.get("/api/v1/inventory/NOPE-0001/nyc/changes")
    assert unknown_sku.status_code == 404
    assert unknown_sku.json()["error"]["code"] == "not_found"

    await create_product(client)
    unknown_store = await client.get(f"/api/v1/inventory/{SKU}/zzz/changes")
    assert unknown_store.status_code == 404

    assert (await client.get(f"/api/v1/inventory/{SKU}/nyc")).status_code == 404
    assert await _changes(client) == []


async def test_seed_writes_no_history(harness) -> None:
    client, app = await harness()
    await seed(app.state.session_factory, now=app.state.clock.now())

    stock = (await client.get(f"/api/v1/inventory/{SKU}/nyc")).json()
    assert (stock["quantity"], stock["version"]) == (12, 1)
    assert await _changes(client) == []
