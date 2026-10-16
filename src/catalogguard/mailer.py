"""Run one email handoff batch and exit. Nothing is sent.

`python -m catalogguard.mailer` decides up to EMAIL_HANDOFF_BATCH_SIZE queued
drafts and writes one email_handoffs row per draft. It is not a daemon. A
scheduler can call it again; a second run skips drafts that already have a
decision. The last stdout line is a JSON summary of the batch.

On SQLite it creates missing tables first, like the API. Postgres relies on
Alembic (`make migrate`), so run that before the first handoff.
"""

import asyncio
import json
from dataclasses import asdict

from catalogguard.application.email_handoff import HandoffBatchResult
from catalogguard.application.ports import Clock
from catalogguard.infrastructure.clock import SystemClock
from catalogguard.infrastructure.config import Settings
from catalogguard.infrastructure.container import build_handoff_service
from catalogguard.infrastructure.db.session import create_db_engine, create_session_factory, init_db
from catalogguard.infrastructure.db.uow import SqlUnitOfWorkFactory
from catalogguard.infrastructure.logging import configure_logging
from catalogguard.infrastructure.metrics import PrometheusMetrics


async def run_once(settings: Settings, *, clock: Clock | None = None) -> HandoffBatchResult:
    engine = create_db_engine(settings)
    try:
        if settings.database_url.startswith("sqlite"):
            await init_db(engine)
        service = build_handoff_service(
            settings,
            SqlUnitOfWorkFactory(create_session_factory(engine)),
            clock=clock or SystemClock(),
            metrics=PrometheusMetrics(),
        )
        return await service.run_batch()
    finally:
        await engine.dispose()


def main() -> None:
    settings = Settings()
    configure_logging(settings.log_level)
    result = asyncio.run(run_once(settings))
    print(json.dumps(asdict(result)))


if __name__ == "__main__":
    main()
