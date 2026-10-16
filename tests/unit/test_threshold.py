from catalogguard.domain.threshold import resolve_threshold


def test_store_override_beats_sku_and_global() -> None:
    decision = resolve_threshold(global_default=5, sku_override=8, store_override=3)
    assert decision.value == 3
    assert decision.source == "sku_store"
    assert decision.sku_store_override == 3
    assert decision.sku_override == 8
    assert decision.global_default == 5


def test_sku_override_used_when_store_override_is_missing() -> None:
    decision = resolve_threshold(global_default=5, sku_override=8, store_override=None)
    assert decision.value == 8
    assert decision.source == "sku"


def test_global_default_when_nothing_is_overridden() -> None:
    decision = resolve_threshold(global_default=5, sku_override=None, store_override=None)
    assert decision.value == 5
    assert decision.source == "global"


def test_explicit_zero_is_a_defined_override() -> None:
    decision = resolve_threshold(global_default=5, sku_override=8, store_override=0)
    assert decision.value == 0
    assert decision.source == "sku_store"
