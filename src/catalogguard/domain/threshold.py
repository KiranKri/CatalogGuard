"""Effective low-stock threshold.

The most specific defined value wins:

1. InventoryLevel.threshold_override for the (SKU, store) pair
2. Product.threshold_override for the SKU
3. The process-wide default from configuration

``None`` means "not defined". A stored ``0`` is defined and wins over a
broader value. Comparison is strict: the pair is low when quantity < threshold.
Threshold 5 alerts at 4 or fewer. Quantity equal to the threshold does not.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ThresholdDecision:
    value: int
    source: str
    global_default: int
    sku_override: int | None
    sku_store_override: int | None


def resolve_threshold(
    *,
    global_default: int,
    sku_override: int | None,
    store_override: int | None,
) -> ThresholdDecision:
    if store_override is not None:
        value = store_override
        source = "sku_store"
    elif sku_override is not None:
        value = sku_override
        source = "sku"
    else:
        value = global_default
        source = "global"
    return ThresholdDecision(
        value=value,
        source=source,
        global_default=global_default,
        sku_override=sku_override,
        sku_store_override=store_override,
    )
