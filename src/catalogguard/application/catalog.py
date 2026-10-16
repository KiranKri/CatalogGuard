"""Product and store use cases."""

from dataclasses import replace
from uuid import uuid4

from catalogguard.application.ports import Clock, UnitOfWorkFactory
from catalogguard.domain.errors import ConflictError, NotFoundError
from catalogguard.domain.models import Product, Store


class ProductService:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def create(self, product: Product) -> Product:
        async with self._uow_factory() as uow:
            if await uow.products.get(product.sku) is not None:
                raise ConflictError(f"Product {product.sku} already exists")
            await uow.products.add(product)
            await uow.commit()
        return product

    async def get(self, sku: str) -> Product:
        async with self._uow_factory() as uow:
            product = await uow.products.get(sku)
        if product is None:
            raise NotFoundError(f"Product {sku} not found")
        return product

    async def list(self, *, limit: int, offset: int) -> list[Product]:
        async with self._uow_factory() as uow:
            return await uow.products.list(limit=limit, offset=offset)

    async def update(self, sku: str, changes: dict[str, object]) -> Product:
        async with self._uow_factory() as uow:
            current = await uow.products.get(sku)
            if current is None:
                raise NotFoundError(f"Product {sku} not found")
            updated = replace(current, **changes, updated_at=self._clock.now())
            await uow.products.update(updated)
            await uow.commit()
        return updated

    async def delete(self, sku: str) -> None:
        async with self._uow_factory() as uow:
            if await uow.products.get(sku) is None:
                raise NotFoundError(f"Product {sku} not found")
            if await uow.inventory.any_for_sku(sku):
                raise ConflictError(f"Product {sku} still has inventory and cannot be deleted")
            await uow.products.delete(sku)
            await uow.commit()


class StoreService:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def create(self, *, store_id: str, name: str, region: str) -> Store:
        store = Store(
            id=store_id,
            name=name,
            region=region,
            created_at=self._clock.now(),
        )
        async with self._uow_factory() as uow:
            if await uow.stores.get(store_id) is not None:
                raise ConflictError(f"Store {store_id} already exists")
            await uow.stores.add(store)
            await uow.commit()
        return store

    async def get(self, store_id: str) -> Store:
        async with self._uow_factory() as uow:
            store = await uow.stores.get(store_id)
        if store is None:
            raise NotFoundError(f"Store {store_id} not found")
        return store

    async def list(self) -> list[Store]:
        async with self._uow_factory() as uow:
            return await uow.stores.list()


def new_id() -> str:
    return str(uuid4())
