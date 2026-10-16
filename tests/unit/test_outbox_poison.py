from datetime import UTC, datetime

from tests.support import create_product, create_store, put_inventory

from catalogguard.infrastructure.db.orm import OutboxORM


async def test_undecodable_outbox_row_does_not_fail_the_write_or_block_the_batch(harness) -> None:
    client, app = await harness()
    await create_store(client)
    await create_product(client)
    async with app.state.session_factory() as session:
        session.add(
            OutboxORM(
                event_id="bad-row",
                event_type="InventoryChanged",
                payload={"event_type": "InventoryChanged"},
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
                published_at=None,
                claimed_at=None,
                claim_token=None,
            )
        )
        await session.commit()

    created = await put_inventory(client, quantity=10, version=0, key="poison-create-001")
    assert created.status_code == 201, created.text
    dropped = await put_inventory(client, quantity=2, version=1, key="poison-drop-00001")
    assert dropped.status_code == 200, dropped.text

    drafts = await client.get("/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc")
    assert len(drafts.json()["items"]) == 1
    letters = (await client.get("/api/v1/dead-letters")).json()["items"]
    assert any(item["handler"] == "outbox_decode" for item in letters)


async def test_correlation_id_longer_than_column_is_rejected(harness) -> None:
    client, _app = await harness()
    response = await client.get("/health", headers={"X-Correlation-ID": "x" * 65})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
