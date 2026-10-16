"""Wall clock. Tests pass a fake clock into the app instead of this one."""

from datetime import UTC, datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
