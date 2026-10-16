from datetime import UTC, datetime, timedelta
from uuid import uuid4

from catalogguard.domain.events import InventoryChanged


def _event() -> InventoryChanged:
    return InventoryChanged(
        event_id=str(uuid4()),
        event_type="InventoryChanged",
        occurred_at=datetime(2026, 6, 1, tzinfo=UTC),
        correlation_id="corr-claim",
        sku="BKPK-28L-OLV",
        store_id="nyc",
        previous_quantity=10,
        new_quantity=2,
        inventory_version=2,
    )


async def _claim(app, now: datetime, token: str):
    async with app.state.uow_factory() as uow:
        rows = await uow.outbox.claim(
            limit=10,
            now=now,
            reclaim_before=now - timedelta(seconds=300),
            token=token,
        )
        await uow.commit()
    return rows


async def test_claim_lease_hides_a_row_until_it_expires_or_is_published(harness) -> None:
    _client, app = await harness()
    async with app.state.uow_factory() as uow:
        await uow.outbox.add(_event())
        await uow.commit()

    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    first = await _claim(app, now, "worker-a")
    assert len(first) == 1
    assert await _claim(app, now, "worker-b") == []

    reclaimed = await _claim(app, now + timedelta(seconds=301), "worker-c")
    assert len(reclaimed) == 1
    assert reclaimed[0].id == first[0].id

    async with app.state.uow_factory() as uow:
        await uow.outbox.mark_published(
            reclaimed[0].id,
            when=now + timedelta(seconds=301),
            claim_token="worker-c",
        )
        await uow.commit()
    later = now + timedelta(hours=2)
    assert await _claim(app, later, "worker-d") == []
