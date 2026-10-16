"""Idempotent demo data. Direct inserts do not emit low-stock events.

Rows that are already below threshold stay quiet until a later update
crosses downward. Re-running the seed does not reset quantities.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from catalogguard.infrastructure.config import Settings
from catalogguard.infrastructure.db.orm import (
    InventoryLevelORM,
    ProductORM,
    RecentInteractionORM,
    StoreORM,
)
from catalogguard.infrastructure.db.session import create_db_engine, create_session_factory, init_db
from catalogguard.infrastructure.logging import configure_logging

STORES = (
    ("nyc", "Hudson Market", "New York"),
    ("chi", "Lakeshore Outfitters", "Chicago"),
    ("aus", "Barton Creek", "Austin"),
)

PRODUCTS: tuple[dict[str, object], ...] = (
    {
        "sku": "BKPK-28L-OLV",
        "name": "Trailhead 28 Pack",
        "brand": "Fieldwork Supply",
        "category": "Packs",
        "attributes": {
            "color": "olive",
            "capacity_l": 28,
            "weight_g": 890,
            "material": "ripstop nylon",
        },
        "base_description": "A 28 liter day pack for the commute and a short trail.",
        "seo_title": "Trailhead 28 Pack | Fieldwork",
        "seo_short_description": "Olive 28 liter day pack.",
        "seo_long_description": "Catalog default long copy for the Trailhead pack.",
        "meta_title": "Trailhead 28 Pack",
        "meta_description": "Fieldwork Supply Trailhead day pack.",
    },
    {
        "sku": "SOCK-MER-3PK",
        "name": "Merino Trail Sock 3-Pack",
        "brand": "Northline",
        "category": "Apparel",
        "attributes": {"size": "M", "material": "merino wool", "pack_count": 3},
        "base_description": "Three pairs of midweight merino socks.",
        "threshold_override": 10,
    },
    {
        "sku": "SKLT-12-BLK",
        "name": "Field Skillet 12 Inch",
        "brand": "Hearth & Co",
        "category": "Cookware",
        "attributes": {"diameter_in": 12, "material": "cast iron", "preseasoned": True},
        "base_description": "A 12 inch skillet for the stove and the camp grate.",
    },
    {
        "sku": "KTL-GOOS-1L",
        "name": "Gooseneck Kettle 1L",
        "brand": "Hearth & Co",
        "category": "Coffee",
        "attributes": {"capacity_ml": 1000, "material": "stainless steel", "flow": "gooseneck"},
        "base_description": "A one liter kettle with a narrow pour.",
    },
    {
        "sku": "SHOE-RUN-M10",
        "name": "Swiftline Runner",
        "brand": "Northline",
        "category": "Footwear",
        "attributes": {"size": "10", "drop_mm": 6, "surface": "road"},
        "base_description": "A road shoe in a men's size 10.",
    },
    {
        "sku": "JKT-DWN-OLV",
        "name": "Ridge Down Jacket",
        "brand": "Fieldwork Supply",
        "category": "Apparel",
        "attributes": {"fill_power": 700, "color": "olive", "weight_g": 380},
        "base_description": "A light down jacket for cool mornings.",
    },
    {
        "sku": "MAT-YOG-5MM",
        "name": "Studio Mat 5mm",
        "brand": "Stillwater",
        "category": "Fitness",
        "attributes": {"thickness_mm": 5, "material": "natural rubber", "length_in": 68},
        "base_description": "A 5 millimeter mat for daily practice.",
    },
    {
        "sku": "MUG-CER-12",
        "name": "Kiln Mug 12oz",
        "brand": "Stillwater",
        "category": "Home",
        "attributes": {"capacity_oz": 12, "material": "stoneware", "dishwasher_safe": True},
        "base_description": "A 12 ounce stoneware mug.",
    },
    {
        "sku": "LANT-LED-01",
        "name": "Camp Lantern 300",
        "brand": "Fieldwork Supply",
        "category": "Lighting",
        "attributes": {"lumens": 300, "runtime_h": 40, "power": "usb-c"},
        "base_description": "A rechargeable lantern rated at 300 lumens.",
    },
    {
        "sku": "BLKT-WOL-QUE",
        "name": "Wool Queen Blanket",
        "brand": "Hearth & Co",
        "category": "Home",
        "attributes": {"size": "queen", "material": "wool", "weight_oz": 48},
        "base_description": "A queen-size wool blanket.",
        "threshold_override": 8,
    },
)

# (store_id, quantity, per-store threshold override or None)
INVENTORY: dict[str, tuple[tuple[str, int, int | None], ...]] = {
    "BKPK-28L-OLV": (("nyc", 12, None), ("chi", 4, None), ("aus", 20, None)),
    "SOCK-MER-3PK": (("nyc", 40, None), ("chi", 18, None), ("aus", 8, None)),
    "SKLT-12-BLK": (("nyc", 14, None), ("chi", 9, None)),
    "KTL-GOOS-1L": (("nyc", 11, None), ("aus", 7, None)),
    "SHOE-RUN-M10": (("nyc", 6, None), ("chi", 6, None), ("aus", 6, None)),
    "JKT-DWN-OLV": (("nyc", 8, None), ("chi", 5, None)),
    "MAT-YOG-5MM": (("nyc", 16, None), ("aus", 10, None)),
    "MUG-CER-12": (("nyc", 30, None), ("chi", 22, None), ("aus", 15, None)),
    "LANT-LED-01": (("nyc", 18, None), ("chi", 7, None)),
    "BLKT-WOL-QUE": (("nyc", 12, None), ("chi", 6, None), ("aus", 20, 3)),
}


def _interactions(now: datetime) -> tuple[dict[str, object], ...]:
    def ago(hours: int) -> datetime:
        return now - timedelta(hours=hours)

    return (
        {
            "id": "int-bkpk-1842-cart",
            "customer_id": "cus_1842",
            "sku": "BKPK-28L-OLV",
            "store_id": "nyc",
            "interaction_type": "add_to_cart",
            "timestamp": ago(2),
        },
        {
            "id": "int-bkpk-9021-view",
            "customer_id": "cus_9021",
            "sku": "BKPK-28L-OLV",
            "store_id": "nyc",
            "interaction_type": "view",
            "timestamp": ago(5),
        },
        {
            "id": "int-bkpk-4410-view",
            "customer_id": "cus_4410",
            "sku": "BKPK-28L-OLV",
            "store_id": "nyc",
            "interaction_type": "view",
            "timestamp": ago(20),
        },
        {
            "id": "int-bkpk-7781-cart",
            "customer_id": "cus_7781",
            "sku": "BKPK-28L-OLV",
            "store_id": "nyc",
            "interaction_type": "add_to_cart",
            "timestamp": ago(8),
        },
        {
            "id": "int-bkpk-1842-view",
            "customer_id": "cus_1842",
            "sku": "BKPK-28L-OLV",
            "store_id": "nyc",
            "interaction_type": "view",
            "timestamp": ago(30),
        },
        {
            "id": "int-bkpk-1009-chi",
            "customer_id": "cus_1009",
            "sku": "BKPK-28L-OLV",
            "store_id": "chi",
            "interaction_type": "view",
            "timestamp": ago(3),
        },
        {
            "id": "int-sklt-2201-cart",
            "customer_id": "cus_2201",
            "sku": "SKLT-12-BLK",
            "store_id": "nyc",
            "interaction_type": "add_to_cart",
            "timestamp": ago(6),
        },
    )


async def seed(session_factory: object, *, now: datetime | None = None) -> None:
    current = now or datetime.now(UTC)
    async with session_factory() as session:  # type: ignore[operator]
        for store_id, name, region in STORES:
            if await session.get(StoreORM, store_id) is None:
                session.add(StoreORM(id=store_id, name=name, region=region, created_at=current))
        await session.flush()
        for spec in PRODUCTS:
            sku = str(spec["sku"])
            if await session.get(ProductORM, sku) is None:
                session.add(
                    ProductORM(
                        sku=sku,
                        name=str(spec["name"]),
                        brand=str(spec["brand"]),
                        category=str(spec["category"]),
                        attributes=dict(spec["attributes"]),  # type: ignore[arg-type]
                        base_description=str(spec["base_description"]),
                        seo_title=str(spec.get("seo_title", "")),
                        seo_short_description=str(spec.get("seo_short_description", "")),
                        seo_long_description=str(spec.get("seo_long_description", "")),
                        meta_title=str(spec.get("meta_title", "")),
                        meta_description=str(spec.get("meta_description", "")),
                        threshold_override=spec.get("threshold_override"),  # type: ignore[arg-type]
                        created_at=current,
                        updated_at=current,
                    )
                )
        await session.flush()
        for sku, rows in INVENTORY.items():
            for store_id, quantity, override in rows:
                if await session.get(InventoryLevelORM, (sku, store_id)) is None:
                    session.add(
                        InventoryLevelORM(
                            sku=sku,
                            store_id=store_id,
                            quantity=quantity,
                            threshold_override=override,
                            updated_at=current,
                            version=1,
                        )
                    )
        await session.flush()
        for item in _interactions(current):
            if await session.get(RecentInteractionORM, item["id"]) is None:
                session.add(
                    RecentInteractionORM(
                        id=str(item["id"]),
                        customer_id=str(item["customer_id"]),
                        sku=str(item["sku"]),
                        store_id=str(item["store_id"]),
                        interaction_type=str(item["interaction_type"]),
                        timestamp=item["timestamp"],
                    )
                )
        await session.commit()


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    engine = create_db_engine(settings)
    asyncio.run(_main(engine, settings))


async def _main(engine: object, settings: Settings) -> None:
    await init_db(engine)  # type: ignore[arg-type]
    factory = create_session_factory(engine)  # type: ignore[arg-type]
    await seed(factory)
    await engine.dispose()  # type: ignore[attr-defined]
    del settings


if __name__ == "__main__":
    main()
