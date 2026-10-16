from catalogguard.seed import seed


async def test_seed_is_idempotent_and_resolves_thresholds(harness) -> None:
    client, app = await harness()
    await seed(app.state.session_factory, now=app.state.clock.now())
    await seed(app.state.session_factory, now=app.state.clock.now())

    products = await client.get("/api/v1/products?limit=50")
    stores = await client.get("/api/v1/stores")
    assert products.status_code == 200
    assert len(products.json()["items"]) == 10
    assert len(stores.json()["items"]) == 3

    global_threshold = await client.get("/api/v1/thresholds/BKPK-28L-OLV/nyc")
    sku_threshold = await client.get("/api/v1/thresholds/BLKT-WOL-QUE/nyc")
    store_threshold = await client.get("/api/v1/thresholds/BLKT-WOL-QUE/aus")
    assert global_threshold.json()["effective_threshold"] == 5
    assert global_threshold.json()["source"] == "global"
    assert sku_threshold.json()["effective_threshold"] == 8
    assert sku_threshold.json()["source"] == "sku"
    assert store_threshold.json()["effective_threshold"] == 3
    assert store_threshold.json()["source"] == "sku_store"

    drafts = await client.get("/api/v1/listings/drafts")
    assert drafts.json()["items"] == []
