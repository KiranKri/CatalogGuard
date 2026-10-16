import asyncio

from tests.support import create_product, create_store, put_inventory


async def _draft_count(client) -> int:
    response = await client.get("/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc")
    assert response.status_code == 200
    return len(response.json()["items"])


async def test_same_key_replays_and_does_not_apply_twice(harness) -> None:
    client, _app = await harness()
    await create_store(client)
    await create_product(client)
    created = await put_inventory(client, quantity=10, version=0, key="idem-create-0001")
    assert created.status_code == 201, created.text

    first = await put_inventory(client, quantity=2, version=1, key="idem-drop-000001")
    assert first.status_code == 200, first.text
    assert await _draft_count(client) == 1

    replay = await put_inventory(client, quantity=2, version=1, key="idem-drop-000001")
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json() == first.json()
    assert await _draft_count(client) == 1

    conflict = await put_inventory(client, quantity=1, version=1, key="idem-drop-000001")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    current = await client.get("/api/v1/inventory/BKPK-28L-OLV/nyc")
    assert current.json()["quantity"] == 2
    assert current.json()["version"] == 2
    assert await _draft_count(client) == 1


async def test_unchanged_quantity_does_not_emit_another_draft(harness) -> None:
    client, _app = await harness()
    await create_store(client)
    await create_product(client)
    await put_inventory(client, quantity=10, version=0, key="idem-create-0002")
    await put_inventory(client, quantity=2, version=1, key="idem-drop-000002")
    same = await put_inventory(client, quantity=2, version=2, key="idem-same-0000001")
    assert same.status_code == 200, same.text
    assert same.json()["version"] == 2
    assert await _draft_count(client) == 1


async def test_concurrent_same_key_replays(harness) -> None:
    client, _app = await harness()
    await create_store(client)
    await create_product(client)
    created = await put_inventory(client, quantity=10, version=0, key="race-create-00001")
    assert created.status_code == 201, created.text
    first, second = await asyncio.gather(
        put_inventory(client, quantity=2, version=1, key="race-drop-0000001"),
        put_inventory(client, quantity=2, version=1, key="race-drop-0000001"),
    )
    assert {first.status_code, second.status_code} == {200}
    assert any(
        response.headers.get("idempotency-replayed") == "true" for response in (first, second)
    )
    drafts = await client.get("/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc")
    assert len(drafts.json()["items"]) == 1


async def test_missing_idempotency_key_is_rejected(harness) -> None:
    client, _app = await harness()
    await create_store(client)
    await create_product(client)
    response = await client.put(
        "/api/v1/inventory/BKPK-28L-OLV/nyc",
        json={"quantity": 10, "expected_version": 0},
    )
    assert response.status_code == 422
