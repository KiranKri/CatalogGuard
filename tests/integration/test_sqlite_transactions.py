"""On SQLite, savepoint writes belong to the unit-of-work transaction and roll back with it."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from catalogguard.domain.errors import ConflictError
from catalogguard.domain.models import InventoryLevel, Product, Store
from catalogguard.infrastructure.config import Settings
from catalogguard.infrastructure.db.session import (
    create_db_engine,
    create_session_factory,
    init_db,
)
from catalogguard.infrastructure.db.uow import SqlUnitOfWork

pytestmark = pytest.mark.sqlite_only

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
SKU = "BKPK-28L-OLV"
HANDLER = "threshold_evaluation"


class _Boom(Exception):
    pass


@pytest.fixture
async def unit_of_work(tmp_path: Path):
    database = (tmp_path / "transactions.db").resolve().as_posix()
    settings = Settings(database_url=f"sqlite+aiosqlite:///{database}", seed_on_startup=False)
    engine = create_db_engine(settings)
    await init_db(engine)
    session_factory = create_session_factory(engine)
    yield lambda: SqlUnitOfWork(session_factory)
    await engine.dispose()


async def _seen(unit_of_work, event_id: str) -> bool:
    async with unit_of_work() as uow:
        return await uow.processed.seen(event_id, HANDLER)


async def test_a_failed_unit_of_work_rolls_back_the_processed_mark(unit_of_work) -> None:
    with pytest.raises(_Boom):
        async with unit_of_work() as uow:
            await uow.processed.mark("event-rolled-back", HANDLER, NOW)
            assert await uow.processed.seen("event-rolled-back", HANDLER)
            raise _Boom

    assert await _seen(unit_of_work, "event-rolled-back") is False


async def test_a_failed_unit_of_work_rolls_back_a_savepoint_inventory_insert(
    unit_of_work,
) -> None:
    async with unit_of_work() as uow:
        await uow.stores.add(
            Store(id="nyc", name="Hudson Market", region="New York", created_at=NOW)
        )
        await uow.products.add(
            Product(
                sku=SKU,
                name="Trailhead 28 Pack",
                brand="Fieldwork Supply",
                category="Packs",
                attributes={},
                base_description="A 28 liter day pack.",
                seo_title="Live title",
                seo_short_description="Live short",
                seo_long_description="Live long",
                meta_title="Live meta",
                meta_description="Live meta description",
                threshold_override=None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        await uow.commit()

    level = InventoryLevel(
        sku=SKU, store_id="nyc", quantity=10, threshold_override=None, updated_at=NOW, version=1
    )
    with pytest.raises(_Boom):
        async with unit_of_work() as uow:
            await uow.inventory.add(level)
            assert await uow.inventory.get(SKU, "nyc") == level
            raise _Boom

    async with unit_of_work() as uow:
        assert await uow.inventory.get(SKU, "nyc") is None
        assert await uow.stores.get("nyc") is not None


async def test_a_successful_unit_of_work_still_commits(unit_of_work) -> None:
    async with unit_of_work() as uow:
        await uow.processed.mark("event-committed", HANDLER, NOW)
        await uow.commit()

    assert await _seen(unit_of_work, "event-committed") is True


async def test_a_conflicting_savepoint_rolls_back_alone(unit_of_work) -> None:
    async with unit_of_work() as uow:
        await uow.processed.mark("event-kept", HANDLER, NOW)
        with pytest.raises(ConflictError):
            await uow.processed.mark("event-kept", HANDLER, NOW)
        await uow.processed.mark("event-after-conflict", HANDLER, NOW)
        await uow.commit()

    assert await _seen(unit_of_work, "event-kept") is True
    assert await _seen(unit_of_work, "event-after-conflict") is True
