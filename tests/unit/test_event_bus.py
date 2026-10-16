from datetime import UTC, datetime
from uuid import uuid4

from catalogguard.domain.events import InventoryChanged
from catalogguard.infrastructure.events.bus import InProcessEventBus


def _event() -> InventoryChanged:
    return InventoryChanged(
        event_id=str(uuid4()),
        event_type="InventoryChanged",
        occurred_at=datetime(2026, 6, 1, tzinfo=UTC),
        correlation_id="corr-bus",
        sku="BKPK-28L-OLV",
        store_id="nyc",
        previous_quantity=10,
        new_quantity=2,
        inventory_version=2,
    )


def _bus(app, retries: int) -> InProcessEventBus:
    return InProcessEventBus(
        uow_factory=app.state.uow_factory,
        metrics=app.state.metrics,
        clock=app.state.clock,
        max_retries=retries,
        backoff_seconds=0,
        mode="inline",
    )


async def _publish(app, bus, event) -> None:
    async with app.state.uow_factory() as uow:
        await bus.publish(event, uow.outbox)
        await uow.commit()


async def test_failing_handler_is_retried_then_succeeds(harness) -> None:
    _client, app = await harness()

    class Flaky:
        name = "flaky"

        def __init__(self) -> None:
            self.calls = 0

        async def handle(self, event) -> None:
            del event
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("nope")

    bus = _bus(app, retries=3)
    flaky = Flaky()
    bus.subscribe("InventoryChanged", flaky)
    await _publish(app, bus, _event())
    await bus.drain()
    await bus.drain()
    assert flaky.calls == 3
    letters = await app.state.container.dead_letters.list(limit=20, offset=0)
    assert letters == []


async def test_exhausted_handler_is_dead_lettered(harness) -> None:
    _client, app = await harness()

    class Always:
        name = "always"

        def __init__(self) -> None:
            self.calls = 0

        async def handle(self, event) -> None:
            del event
            self.calls += 1
            raise RuntimeError("boom")

    bus = _bus(app, retries=2)
    handler = Always()
    bus.subscribe("InventoryChanged", handler)
    await _publish(app, bus, _event())
    await bus.drain()
    assert handler.calls == 2
    letters = await app.state.container.dead_letters.list(limit=20, offset=0)
    assert len(letters) == 1
    assert letters[0].handler == "always"
    assert letters[0].attempts == 2
    assert "boom" in letters[0].error
