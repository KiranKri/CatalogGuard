"""Inventory upsert: optimistic version, idempotency key, outbox event."""

import logging
from uuid import uuid4

from catalogguard.application.commands import InventoryUpdateResult, UpsertInventory
from catalogguard.application.ports import Clock, EventBus, UnitOfWorkFactory
from catalogguard.domain.errors import (
    ConflictError,
    IdempotencyConflictError,
    NotFoundError,
    OptimisticConflictError,
)
from catalogguard.domain.events import InventoryChanged
from catalogguard.domain.models import IdempotencyRecord, InventoryLevel

log = logging.getLogger(__name__)


def _body(level: InventoryLevel, *, previous_quantity: int | None) -> dict[str, object]:
    return {
        "sku": level.sku,
        "store_id": level.store_id,
        "quantity": level.quantity,
        "version": level.version,
        "threshold_override": level.threshold_override,
        "updated_at": level.updated_at.isoformat(),
        "previous_quantity": previous_quantity,
    }


class InventoryService:
    def __init__(self, uow_factory: UnitOfWorkFactory, bus: EventBus, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._bus = bus
        self._clock = clock

    async def upsert(self, cmd: UpsertInventory) -> InventoryUpdateResult:
        try:
            replay, result = await self._write(cmd)
        except (OptimisticConflictError, ConflictError):
            recovered = await self._replay(cmd)
            if recovered is None:
                raise
            await self._drain_quietly()
            return recovered
        await self._drain_quietly()
        if replay is not None:
            return replay
        assert result is not None
        return result

    async def _write(
        self, cmd: UpsertInventory
    ) -> tuple[InventoryUpdateResult | None, InventoryUpdateResult | None]:
        replay: InventoryUpdateResult | None = None
        result: InventoryUpdateResult | None = None
        async with self._uow_factory() as uow:
            existing = await uow.idempotency.get(cmd.idempotency_key)
            if existing is not None:
                if existing.request_hash != cmd.request_hash:
                    raise IdempotencyConflictError(
                        "Idempotency key was already used with a different request"
                    )
                replay = InventoryUpdateResult(
                    body=dict(existing.response_body),
                    status_code=existing.response_code,
                    replayed=True,
                )
            else:
                result = await self._apply(uow, cmd)
        return replay, result

    async def _replay(self, cmd: UpsertInventory) -> InventoryUpdateResult | None:
        async with self._uow_factory() as uow:
            existing = await uow.idempotency.get(cmd.idempotency_key)
        if existing is None:
            return None
        if existing.request_hash != cmd.request_hash:
            raise IdempotencyConflictError(
                "Idempotency key was already used with a different request"
            )
        return InventoryUpdateResult(
            body=dict(existing.response_body),
            status_code=existing.response_code,
            replayed=True,
        )

    async def _drain_quietly(self) -> None:
        try:
            await self._bus.drain()
        except Exception:
            log.exception("drain_failed_after_commit")

    async def _apply(self, uow, cmd: UpsertInventory) -> InventoryUpdateResult:
        if await uow.products.get(cmd.sku) is None:
            raise NotFoundError(f"Product {cmd.sku} not found")
        if await uow.stores.get(cmd.store_id) is None:
            raise NotFoundError(f"Store {cmd.store_id} not found")

        current = await uow.inventory.get(cmd.sku, cmd.store_id)
        now = self._clock.now()
        if current is None:
            if cmd.expected_version != 0:
                raise OptimisticConflictError(
                    "Inventory row does not exist; expected_version must be 0 to create it",
                    current_version=0,
                )
            override = cmd.threshold_override if cmd.threshold_override_provided else None
            saved = InventoryLevel(
                sku=cmd.sku,
                store_id=cmd.store_id,
                quantity=cmd.quantity,
                threshold_override=override,
                updated_at=now,
                version=1,
            )
            await uow.inventory.add(saved)
            previous: int | None = None
            changed = True
            status_code = 201
        else:
            if cmd.expected_version != current.version:
                raise OptimisticConflictError(
                    "Inventory version does not match",
                    current_version=current.version,
                )
            override = (
                cmd.threshold_override
                if cmd.threshold_override_provided
                else current.threshold_override
            )
            changed = current.quantity != cmd.quantity or current.threshold_override != override
            saved = InventoryLevel(
                sku=current.sku,
                store_id=current.store_id,
                quantity=cmd.quantity,
                threshold_override=override,
                updated_at=now if changed else current.updated_at,
                version=current.version + 1 if changed else current.version,
            )
            if changed:
                ok = await uow.inventory.compare_and_set(
                    expected_version=current.version,
                    level=saved,
                )
                if not ok:
                    latest = await uow.inventory.get(cmd.sku, cmd.store_id)
                    raise OptimisticConflictError(
                        "Inventory version does not match",
                        current_version=latest.version if latest is not None else 0,
                    )
            previous = current.quantity
            status_code = 200

        body = _body(saved, previous_quantity=previous)
        await uow.idempotency.add(
            IdempotencyRecord(
                key=cmd.idempotency_key,
                request_hash=cmd.request_hash,
                response_code=status_code,
                response_body=body,
                created_at=now,
            )
        )
        if changed:
            event = InventoryChanged(
                event_id=str(uuid4()),
                event_type="InventoryChanged",
                occurred_at=now,
                correlation_id=cmd.correlation_id,
                sku=cmd.sku,
                store_id=cmd.store_id,
                previous_quantity=previous,
                new_quantity=saved.quantity,
                inventory_version=saved.version,
            )
            await self._bus.publish(event, uow.outbox)
        await uow.commit()
        log.info(
            "inventory_upserted sku=%s store=%s quantity=%s version=%s previous=%s",
            saved.sku,
            saved.store_id,
            saved.quantity,
            saved.version,
            previous,
        )
        return InventoryUpdateResult(body=body, status_code=status_code, replayed=False)

    async def get(self, sku: str, store_id: str) -> InventoryLevel:
        async with self._uow_factory() as uow:
            level = await uow.inventory.get(sku, store_id)
        if level is None:
            raise NotFoundError(f"Inventory for {sku} at {store_id} not found")
        return level

    async def list(
        self, *, sku: str | None, store_id: str | None, limit: int, offset: int
    ) -> list[InventoryLevel]:
        async with self._uow_factory() as uow:
            return await uow.inventory.list(sku=sku, store_id=store_id, limit=limit, offset=offset)
