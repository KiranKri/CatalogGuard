"""JSON responses name UTF-8 so clients do not guess Latin-1."""

from tests.support import create_product, create_store, put_inventory

_JSON = "application/json; charset=utf-8"


async def test_success_conflict_and_validation_json_declare_utf8(harness) -> None:
    client, _app = await harness()
    health = await client.get("/health")
    assert health.status_code == 200
    assert health.headers["content-type"] == _JSON

    await create_store(client)
    await create_product(client)
    created = await put_inventory(client, quantity=10, version=0, key="charset-create-0001")
    assert created.status_code == 201
    assert created.headers["content-type"] == _JSON

    stale = await put_inventory(client, quantity=2, version=0, key="charset-stale-00001")
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "optimistic_conflict"
    assert stale.headers["content-type"] == _JSON

    missing_key = await client.put(
        "/api/v1/inventory/BKPK-28L-OLV/nyc",
        json={"quantity": 2, "expected_version": 1},
    )
    assert missing_key.status_code == 422
    assert missing_key.headers["content-type"] == _JSON

    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    assert not metrics.headers["content-type"].lower().startswith("application/json")
