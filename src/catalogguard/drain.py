"""Drain the outbox once and exit.

`python -m catalogguard.drain` (or `make drain`) builds the same handlers and
bus as the API, runs the drain inline until no claimable row is left, prints a
JSON summary as the last stdout line, and exits. It is not a daemon. A
scheduler can call it again. Use it when the API runs EVENT_DISPATCH=async, or
after a crash left unpublished rows.

The worker always drains inline, whatever EVENT_DISPATCH says, because an async
drain would return before the work is done. On SQLite it creates missing tables
first. Postgres relies on Alembic (`make migrate`). Do not run it next to an API
process on the same SQLite file.
"""

import asyncio
import json
from dataclasses import asdict, dataclass

from catalogguard.application.ports import Clock
from catalogguard.infrastructure.config import Settings
from catalogguard.infrastructure.container import build_runtime
from catalogguard.infrastructure.db.session import init_db
from catalogguard.infrastructure.logging import configure_logging


@dataclass(frozen=True)
class DrainResult:
    unpublished_before: int
    unpublished_after: int


async def run_once(settings: Settings, *, clock: Clock | None = None) -> DrainResult:
    worker_settings = settings.model_copy(update={"event_dispatch": "inline"})
    runtime = build_runtime(worker_settings, clock=clock)
    try:
        if worker_settings.database_url.startswith("sqlite"):
            await init_db(runtime.engine)
        before = await runtime.container.outbox.summary()
        await runtime.bus.drain()
        after = await runtime.container.outbox.summary()
        return DrainResult(
            unpublished_before=before.unpublished,
            unpublished_after=after.unpublished,
        )
    finally:
        await runtime.llm.aclose()
        await runtime.engine.dispose()


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    result = asyncio.run(run_once(settings))
    print(json.dumps(asdict(result)))


if __name__ == "__main__":
    main()
