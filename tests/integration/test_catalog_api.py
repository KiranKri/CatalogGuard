from tests.support import create_product, create_store, put_inventory


async def test_product_and_store_crud(harness) -> None:
    client, _app = await harness()
    await create_store(client)
    created = await create_product(client)
    assert created.json()["sku"] == "BKPK-28L-OLV"

    duplicate = await client.post(
        "/api/v1/products",
        json={
            "sku": "BKPK-28L-OLV",
            "name": "Other",
            "brand": "Other",
            "category": "Other",
            "attributes": {},
            "base_description": "Other",
        },
    )
    assert duplicate.status_code == 409

    patched = await client.patch(
        "/api/v1/products/BKPK-28L-OLV",
        json={"name": "Trailhead Pack"},
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Trailhead Pack"

    listed = await client.get("/api/v1/products")
    assert listed.json()["items"][0]["sku"] == "BKPK-28L-OLV"

    blocked = await put_inventory(client, quantity=4, version=0, key="crud-inventory-01")
    assert blocked.status_code == 201
    denied = await client.delete("/api/v1/products/BKPK-28L-OLV")
    assert denied.status_code == 409

    await create_product(client, sku="MUG-CER-12", name="Kiln Mug 12oz")
    deleted = await client.delete("/api/v1/products/MUG-CER-12")
    assert deleted.status_code == 204
    missing = await client.get("/api/v1/products/MUG-CER-12")
    assert missing.status_code == 404
