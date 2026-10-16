from datetime import timedelta

from tests.support import create_product, create_store, put_inventory


async def _counts(client) -> tuple[int, int]:
    drafts = await client.get("/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc")
    emails = await client.get("/api/v1/email-drafts?sku=BKPK-28L-OLV&store_id=nyc")
    return len(drafts.json()["items"]), len(emails.json()["items"])


async def test_sellout_does_not_queue_an_act_now_email(harness) -> None:
    client, _app = await harness()
    await create_store(client)
    await create_product(client)
    created = await put_inventory(client, quantity=10, version=0, key="sold-create-0001")
    assert created.status_code == 201, created.text
    sold = await put_inventory(client, quantity=0, version=1, key="sold-out-0000001")
    assert sold.status_code == 200, sold.text
    drafts, emails = await _counts(client)
    assert drafts == 1
    assert emails == 0


async def test_first_write_below_threshold_is_not_a_crossing(harness) -> None:
    client, _app = await harness()
    await create_store(client)
    await create_product(client)
    created = await put_inventory(client, quantity=1, version=0, key="flap-create-0001")
    assert created.status_code == 201, created.text
    assert await _counts(client) == (0, 0)
    lower = await put_inventory(client, quantity=0, version=1, key="flap-lower-00001")
    assert lower.status_code == 200, lower.text
    assert await _counts(client) == (0, 0)


async def test_only_a_crossing_outside_cooldown_fires(harness) -> None:
    client, app = await harness()
    await create_store(client)
    await create_product(client)

    assert (
        await put_inventory(client, quantity=10, version=0, key="flap-create-0002")
    ).status_code == 201
    assert await _counts(client) == (0, 0)

    crossed = await put_inventory(client, quantity=4, version=1, key="flap-cross-00001")
    assert crossed.status_code == 200, crossed.text
    assert await _counts(client) == (1, 1)

    still_low = await put_inventory(client, quantity=2, version=2, key="flap-still-00001")
    assert still_low.status_code == 200, still_low.text
    assert await _counts(client) == (1, 1)

    restock = await put_inventory(client, quantity=10, version=3, key="flap-restock-0001")
    assert restock.status_code == 200, restock.text
    assert await _counts(client) == (1, 1)

    suppressed = await put_inventory(client, quantity=1, version=4, key="flap-suppress-001")
    assert suppressed.status_code == 200, suppressed.text
    assert await _counts(client) == (1, 1)

    app.state.clock.advance(timedelta(hours=24))
    above = await put_inventory(client, quantity=10, version=5, key="flap-above-000001")
    assert above.status_code == 200, above.text
    assert await _counts(client) == (1, 1)

    again = await put_inventory(client, quantity=3, version=6, key="flap-again-000001")
    assert again.status_code == 200, again.text
    assert await _counts(client) == (2, 2)
