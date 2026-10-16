"""Read the effective threshold for a SKU in a store."""

from catalogguard.application.ports import UnitOfWorkFactory
from catalogguard.domain.errors import NotFoundError
from catalogguard.domain.threshold import ThresholdDecision, resolve_threshold


class ThresholdService:
    def __init__(self, uow_factory: UnitOfWorkFactory, *, default_threshold: int) -> None:
        self._uow_factory = uow_factory
        self._default_threshold = default_threshold

    async def get(self, sku: str, store_id: str) -> ThresholdDecision:
        async with self._uow_factory() as uow:
            product = await uow.products.get(sku)
            if product is None:
                raise NotFoundError(f"Product {sku} not found")
            store = await uow.stores.get(store_id)
            if store is None:
                raise NotFoundError(f"Store {store_id} not found")
            inventory = await uow.inventory.get(sku, store_id)
        store_override = None if inventory is None else inventory.threshold_override
        return resolve_threshold(
            global_default=self._default_threshold,
            sku_override=product.threshold_override,
            store_override=store_override,
        )
