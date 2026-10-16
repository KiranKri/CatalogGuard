"""Retry transient LLM failures and record attempt latency."""

import time

from catalogguard.application.ports import Metrics
from catalogguard.infrastructure.llm.errors import TransientLLMError
from catalogguard.infrastructure.llm.types import LLMResult
from catalogguard.infrastructure.retry import retry_async


class RetryingLLM:
    def __init__(
        self,
        inner: object,
        *,
        attempts: int,
        backoff_seconds: float,
        metrics: Metrics,
    ) -> None:
        self._inner = inner
        self._attempts = attempts
        self._backoff = backoff_seconds
        self._metrics = metrics

    async def complete(self, *, system: str, user: str) -> LLMResult:
        async def operation() -> LLMResult:
            started = time.perf_counter()
            try:
                return await self._inner.complete(system=system, user=user)
            finally:
                self._metrics.observe_llm(time.perf_counter() - started)

        return await retry_async(
            operation,
            attempts=self._attempts,
            base_delay=self._backoff,
            retry_on=(TransientLLMError,),
        )

    async def aclose(self) -> None:
        closer = getattr(self._inner, "aclose", None)
        if closer is not None:
            await closer()
