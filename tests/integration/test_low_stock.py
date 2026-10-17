"""Low-stock board and the draft-to-inventory-write cause link."""

import uuid
from dataclasses import replace
from datetime import timedelta

from sqlalchemy import delete, update
from tests.support import create_product, create_store, put_inventory

from catalogguard.infrastructure.db.orm import InventoryChangeORM, OutboxORM
from catalogguard.seed import seed

SKU = "BKPK-28L-OLV"
MUG = "MUG-CER-12"
LOW_STOCK_NAMESPACE = uuid.UUID("6f1d8c2a-4b7e-5d19-8c33-1a2b3c4d5e6f")
BOARD_FIELDS = {
    "sku",
    "store_id",
    "quantity",
    "inventory_version",
    "threshold",
    "threshold_source",
    "latest_listing_draft_id",
    "latest_listing_status",
    "latest_email_draft_id",
    "latest_email_status",
}


async def _board(client, store_id: str = "nyc", query: str = "") -> list[dict]:
    response = await client.get(f"/api/v1/stores/{store_id}/low-stock{query}")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    for item in items:
        assert set(item) == BOARD_FIELDS
    return items


async def _put(client, sku: str, quantity: int, version: int, key: str, **kwargs) -> None:
    response = await put_inventory(
        client, sku=sku, quantity=quantity, version=version, key=key, **kwargs
    )
    assert response.status_code in {200, 201}, response.text


async def _drafts(client, sku: str = SKU, store_id: str = "nyc") -> list[dict]:
    url = f"/api/v1/listings/drafts?sku={sku}&store_id={store_id}"
    return (await client.get(url)).json()["items"]


async def _emails(client, sku: str = SKU, store_id: str = "nyc") -> list[dict]:
    url = f"/api/v1/email-drafts?sku={sku}&store_id={store_id}"
    return (await client.get(url)).json()["items"]


async def _cause(client, draft_id: str) -> dict:
    response = await client.get(f"/api/v1/listings/drafts/{draft_id}/cause")
    assert response.status_code == 200, response.text
    return response.json()


async def _crossed(harness):
    client, app = await harness()
    await create_store(client)
    await create_product(client)
    await _put(client, SKU, 10, 0, "cause-create-001")
    # Distinct instants, so "newest change" does not fall back to the id tie-break.
    app.state.clock.advance(timedelta(minutes=1))
    await _put(client, SKU, 4, 1, "cause-drop-00001")
    [draft] = await _drafts(client)
    return client, app, draft


async def test_board_lists_rows_below_the_effective_threshold(harness) -> None:
    client, app = await harness()
    clock = app.state.clock
    await create_store(client)
    await create_product(client)
    await create_product(client, sku=MUG, name="Kiln Mug 12oz")
    await _put(client, SKU, 10, 0, "board-hero-create")
    await _put(client, MUG, 10, 0, "board-mug-create1")
    assert await _board(client) == []

    await _put(client, SKU, 5, 1, "board-hero-five01")
    assert await _board(client) == []

    await _put(client, SKU, 10, 2, "board-hero-ten001")
    await _put(client, SKU, 4, 3, "board-hero-four01")
    [draft] = await _drafts(client)
    [email] = await _emails(client)
    assert await _board(client) == [
        {
            "sku": SKU,
            "store_id": "nyc",
            "quantity": 4,
            "inventory_version": 4,
            "threshold": 5,
            "threshold_source": "global",
            "latest_listing_draft_id": draft["id"],
            "latest_listing_status": "ready",
            "latest_email_draft_id": email["id"],
            "latest_email_status": "queued",
        }
    ]

    clock.advance(timedelta(minutes=1))
    manual = await client.post(f"/api/v1/listings/{SKU}/stores/nyc/rewrite")
    assert manual.status_code == 201, manual.text
    [row] = await _board(client)
    assert row["latest_listing_draft_id"] == manual.json()["id"]
    assert row["latest_email_draft_id"] == email["id"]

    # Same created_at as the newest draft: the greater id wins.
    async with app.state.uow_factory() as uow:
        newest = await uow.drafts.get(manual.json()["id"])
        twin = replace(
            newest,
            id="zzzzzzzz-tie-break-draft",
            version=newest.version + 1,
            trigger_event_id=str(uuid.uuid4()),
        )
        await uow.drafts.add(twin)
        await uow.commit()
    [row] = await _board(client)
    assert row["latest_listing_draft_id"] == "zzzzzzzz-tie-break-draft"


async def test_zero_override_is_not_low_and_a_sellout_has_no_email(harness) -> None:
    client, _app = await harness()
    await create_store(client)
    await create_product(client)
    await create_product(client, sku=MUG, name="Kiln Mug 12oz")
    await _put(client, SKU, 10, 0, "zero-hero-create1", threshold_override=0)
    await _put(client, SKU, 0, 1, "zero-hero-empty01")
    await _put(client, MUG, 10, 0, "zero-mug-create01")
    await _put(client, MUG, 0, 1, "zero-mug-sellout1")

    [row] = await _board(client)

    assert row["sku"] == MUG
    assert (row["quantity"], row["threshold"], row["threshold_source"]) == (0, 5, "global")
    assert row["latest_listing_status"] == "ready"
    assert row["latest_listing_draft_id"] == (await _drafts(client, MUG))[0]["id"]
    assert (row["latest_email_draft_id"], row["latest_email_status"]) == (None, None)


async def test_board_resolves_all_three_threshold_levels(harness) -> None:
    client, app = await harness()
    await seed(app.state.session_factory, now=app.state.clock.now())

    chicago = await _board(client, "chi")
    assert [(r["sku"], r["quantity"], r["threshold"], r["threshold_source"]) for r in chicago] == [
        (SKU, 4, 5, "global"),
        ("BLKT-WOL-QUE", 6, 8, "sku"),
    ]
    assert all(row["latest_listing_draft_id"] is None for row in chicago)
    second = await _board(client, "chi", "?limit=1&offset=1")
    assert [row["sku"] for row in second] == ["BLKT-WOL-QUE"]
    assert await _board(client, "nyc") == []

    austin = await _board(client, "aus")
    assert [(r["sku"], r["threshold"], r["threshold_source"]) for r in austin] == [
        ("SOCK-MER-3PK", 10, "sku"),
    ]
    await _put(client, "BLKT-WOL-QUE", 2, 1, "board-blanket-aus", store_id="aus")
    austin = await _board(client, "aus")
    blanket = austin[0]
    assert (blanket["sku"], blanket["quantity"]) == ("BLKT-WOL-QUE", 2)
    assert (blanket["threshold"], blanket["threshold_source"]) == (3, "sku_store")
    assert blanket["latest_listing_status"] == "ready"
    assert blanket["latest_email_status"] == "queued"
    assert [row["sku"] for row in austin] == ["BLKT-WOL-QUE", "SOCK-MER-3PK"]


async def test_unknown_store_is_404_and_an_empty_store_is_an_empty_page(harness) -> None:
    client, _app = await harness()
    missing = await client.get("/api/v1/stores/zzz/low-stock")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"

    await create_store(client)
    assert await _board(client) == []


async def test_low_stock_draft_cause_points_at_the_inventory_write(harness) -> None:
    client, _app, draft = await _crossed(harness)

    cause = await _cause(client, draft["id"])

    newest_change = (await client.get(f"/api/v1/inventory/{SKU}/nyc/changes")).json()["items"][0]
    assert cause["draft_id"] == draft["id"]
    assert cause["reason"] == "low_inventory"
    assert cause["low_stock_event_id"] == draft["trigger_event_id"]
    assert cause["inventory_change"] == newest_change
    assert cause["inventory_change"]["new_quantity"] == 4
    change_event = cause["inventory_change"]["event_id"]
    assert change_event != cause["low_stock_event_id"]
    assert cause["low_stock_event_id"] == str(uuid.uuid5(LOW_STOCK_NAMESPACE, change_event))


async def test_manual_draft_has_no_cause_and_unknown_draft_is_404(harness) -> None:
    client, _app = await harness()
    await create_store(client)
    await create_product(client)
    manual = await client.post(f"/api/v1/listings/{SKU}/stores/nyc/rewrite")
    assert manual.status_code == 201, manual.text

    assert await _cause(client, manual.json()["id"]) == {
        "draft_id": manual.json()["id"],
        "reason": "manual",
        "low_stock_event_id": None,
        "inventory_change": None,
    }
    missing = await client.get("/api/v1/listings/drafts/no-such-draft/cause")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_missing_or_mismatched_links_give_a_null_change(harness) -> None:
    client, app, draft = await _crossed(harness)
    change_event = (await _cause(client, draft["id"]))["inventory_change"]["event_id"]

    async def execute(statement) -> None:
        async with app.state.session_factory() as session:
            await session.execute(statement)
            await session.commit()

    row = InventoryChangeORM.event_id == change_event
    await execute(update(InventoryChangeORM).where(row).values(store_id="chi"))
    mismatched = await _cause(client, draft["id"])
    assert mismatched["inventory_change"] is None
    assert mismatched["low_stock_event_id"] == draft["trigger_event_id"]

    await execute(update(InventoryChangeORM).where(row).values(store_id="nyc"))
    assert (await _cause(client, draft["id"]))["inventory_change"] is not None

    await execute(delete(InventoryChangeORM).where(row))
    assert (await _cause(client, draft["id"]))["inventory_change"] is None

    await execute(delete(OutboxORM).where(OutboxORM.event_id == draft["trigger_event_id"]))
    no_outbox = await _cause(client, draft["id"])
    assert no_outbox["inventory_change"] is None
    assert no_outbox["low_stock_event_id"] == draft["trigger_event_id"]
