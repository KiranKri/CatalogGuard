from datetime import UTC, datetime, timedelta

from catalogguard.domain.crossing import is_cooldown_active, is_downward_crossing


def test_downward_crossing_edges() -> None:
    assert is_downward_crossing(previous=None, new=1, threshold=5) is False
    assert is_downward_crossing(previous=10, new=4, threshold=5) is True
    assert is_downward_crossing(previous=10, new=5, threshold=5) is False
    assert is_downward_crossing(previous=5, new=4, threshold=5) is True
    assert is_downward_crossing(previous=4, new=2, threshold=5) is False
    assert is_downward_crossing(previous=4, new=9, threshold=5) is False
    assert is_downward_crossing(previous=9, new=9, threshold=5) is False


def test_cooldown_is_exclusive_at_the_window_end() -> None:
    last = datetime(2026, 6, 1, tzinfo=UTC)
    window = timedelta(hours=24)
    assert is_cooldown_active(last_triggered_at=None, now=last, cooldown=window) is False
    assert (
        is_cooldown_active(
            last_triggered_at=last,
            now=last + timedelta(hours=24) - timedelta(seconds=1),
            cooldown=window,
        )
        is True
    )
    assert (
        is_cooldown_active(last_triggered_at=last, now=last + timedelta(hours=24), cooldown=window)
        is False
    )
