from tests.support import create_product, create_store, put_inventory


async def test_daily_cap_blocks_a_second_rewrite_and_the_event_handler(harness) -> None:
    client, _app = await harness(llm_daily_cap_per_sku=1)
    await create_store(client)
    await create_product(client)

    first = await client.post("/api/v1/listings/BKPK-28L-OLV/stores/nyc/rewrite")
    assert first.status_code == 201, first.text

    second = await client.post("/api/v1/listings/BKPK-28L-OLV/stores/nyc/rewrite")
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "llm_cost_limit"

    created = await put_inventory(client, quantity=10, version=0, key="cap-create-000001")
    assert created.status_code == 201, created.text
    dropped = await put_inventory(client, quantity=2, version=1, key="cap-drop-00000001")
    assert dropped.status_code == 200, dropped.text

    drafts = (await client.get("/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc")).json()
    assert len(drafts["items"]) == 1
    letters = (await client.get("/api/v1/dead-letters")).json()["items"]
    assert [item["handler"] for item in letters] == ["listing_rewrite"]
    assert "cap" in letters[0]["error"]
    emails = (await client.get("/api/v1/email-drafts?sku=BKPK-28L-OLV&store_id=nyc")).json()
    assert len(emails["items"]) == 1
