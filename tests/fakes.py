"""Test doubles. No network."""

from datetime import datetime, timedelta


class FakeClock:
    def __init__(self, current: datetime) -> None:
        self._current = current

    def now(self) -> datetime:
        return self._current

    def advance(self, delta: timedelta) -> None:
        self._current += delta


class FixedLLM:
    def __init__(self, content: str | Exception) -> None:
        self._content = content
        self.calls = 0
        self.prompts: list[str] = []

    async def complete(self, *, system: str, user: str):
        del system
        self.calls += 1
        self.prompts.append(user)
        if isinstance(self._content, Exception):
            raise self._content
        return _Result(self._content, "fixed-test")

    async def aclose(self) -> None:
        return None


class _Result:
    def __init__(self, content: str, model: str) -> None:
        self.content = content
        self.model = model
