from tests.support import create_product, create_store, put_inventory


async def test_stale_version_does_not_overwrite(harness) -> None:
    client, _app = await harness()
    await create_store(client)
    await create_product(client)

    created = await put_inventory(client, quantity=10, version=0, key="conc-create-0001")
    assert created.status_code == 201, created.text
    assert created.json()["version"] == 1

    updated = await put_inventory(client, quantity=4, version=1, key="conc-update-0001")
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert updated.json()["quantity"] == 4

    stale = await put_inventory(client, quantity=1, version=1, key="conc-stale-00001")
    assert stale.status_code == 409
    body = stale.json()["error"]
    assert body["code"] == "optimistic_conflict"
    assert body["current_version"] == 2

    current = await client.get("/api/v1/inventory/BKPK-28L-OLV/nyc")
    assert current.status_code == 200
    assert current.json()["quantity"] == 4
    assert current.json()["version"] == 2


async def test_create_requires_expected_version_zero(harness) -> None:
    client, _app = await harness()
    await create_store(client)
    await create_product(client)
    response = await put_inventory(client, quantity=3, version=2, key="conc-missing-0001")
    assert response.status_code == 409
    assert response.json()["error"]["current_version"] == 0
    missing = await client.get("/api/v1/inventory/BKPK-28L-OLV/nyc")
    assert missing.status_code == 404
