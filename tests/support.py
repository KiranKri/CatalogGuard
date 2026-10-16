"""HTTP helpers shared by service tests."""

from httpx import Response


def product_body(**overrides) -> dict:
    body = {
        "sku": "BKPK-28L-OLV",
        "name": "Trailhead 28 Pack",
        "brand": "Fieldwork Supply",
        "category": "Packs",
        "attributes": {
            "color": "olive",
            "capacity_l": 28,
            "weight_g": 890,
            "material": "ripstop nylon",
        },
        "base_description": "A 28 liter day pack.",
        "seo_title": "Live title",
        "seo_short_description": "Live short",
        "seo_long_description": "Live long",
        "meta_title": "Live meta",
        "meta_description": "Live meta description",
    }
    body.update(overrides)
    return body


async def create_store(client, store_id: str = "nyc", name: str = "Hudson Market") -> Response:
    response = await client.post(
        "/api/v1/stores",
        json={"id": store_id, "name": name, "region": "New York"},
    )
    assert response.status_code == 201, response.text
    return response


async def create_product(client, **overrides) -> Response:
    response = await client.post("/api/v1/products", json=product_body(**overrides))
    assert response.status_code == 201, response.text
    return response


async def put_inventory(
    client,
    *,
    sku: str = "BKPK-28L-OLV",
    store_id: str = "nyc",
    quantity: int,
    version: int,
    key: str,
    threshold_override: object = ...,
) -> Response:
    body: dict[str, object] = {"quantity": quantity, "expected_version": version}
    if threshold_override is not ...:
        body["threshold_override"] = threshold_override
    return await client.put(
        f"/api/v1/inventory/{sku}/{store_id}",
        json=body,
        headers={"Idempotency-Key": key},
    )
