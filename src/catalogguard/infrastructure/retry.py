"""Small async retry helper. Delay doubles each attempt. Zero delay skips sleep."""

import asyncio
from collections.abc import Awaitable, Callable


async def retry_async[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float,
    retry_on: tuple[type[BaseException], ...],
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except retry_on as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            if on_retry is not None:
                on_retry(attempt, exc)
            delay = base_delay * (2 ** (attempt - 1))
            if delay > 0:
                await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error
