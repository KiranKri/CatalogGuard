"""In-process bus. Events are durable in event_outbox before handlers run.

Drain leases rows (`claimed_at`) before dispatch. On Postgres the lease uses
SELECT … FOR UPDATE SKIP LOCKED, so more than one process can drain one
database. A lease older than the claim timeout can be taken again. Kafka is
still the path when consumers should run outside the API process. See
adr/0001-kafka-event-bus.md.

Handlers for one event run concurrently. Failures retry with backoff, then
land in dead_letters. The outbox row is marked published after that so a
permanent failure cannot wedge the drain.
"""

import asyncio
import logging
from datetime import timedelta
from uuid import uuid4

import structlog

from catalogguard.application.ports import Metrics, OutboxMessage, UnitOfWorkFactory
from catalogguard.domain.errors import CostLimitError, NonRetryableError
from catalogguard.domain.events import DomainEvent
from catalogguard.domain.models import DeadLetter
from catalogguard.infrastructure.clock import SystemClock
from catalogguard.infrastructure.events.serialize import dump_event, load_event

log = logging.getLogger(__name__)


def handler_name(handler: object) -> str:
    """The name stored on dead letters, and the key a redrive looks up."""

    return str(getattr(handler, "name", handler.__class__.__name__))


class InProcessEventBus:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        metrics: Metrics,
        clock: SystemClock | object,
        max_retries: int,
        backoff_seconds: float,
        mode: str,
        claim_timeout_seconds: int = 300,
    ) -> None:
        if mode not in {"inline", "async"}:
            raise ValueError("event dispatch mode must be inline or async")
        if max_retries < 1:
            raise ValueError("handler retries must be at least 1")
        self._uow_factory = uow_factory
        self._metrics = metrics
        self._clock = clock
        self._max_retries = max_retries
        self._backoff = backoff_seconds
        self._mode = mode
        self._claim_timeout = timedelta(seconds=claim_timeout_seconds)
        self._handlers: dict[str, list[object]] = {}
        self._lock = asyncio.Lock()
        self._drain_task: asyncio.Task[None] | None = None
        self._wake = False

    def subscribe(self, event_type: str, handler: object) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def handler_for(self, event_type: str, name: str) -> object | None:
        """The subscribed instance named `name` for `event_type`, or None."""

        for handler in self._handlers.get(event_type, []):
            if handler_name(handler) == name:
                return handler
        return None

    async def publish(self, event: DomainEvent, outbox: object) -> None:
        await outbox.add(event)  # type: ignore[attr-defined]
        self._metrics.event_emitted(event.event_type)
        log.info("event_published type=%s id=%s", event.event_type, event.event_id)

    async def drain(self) -> None:
        if self._mode == "async":
            self._wake = True
            if self._drain_task is None or self._drain_task.done():
                self._drain_task = asyncio.create_task(self._drain_until_idle())
            return
        try:
            await self._drain()
        except Exception:
            log.exception("drain_failed")
            self._metrics.drain_failure()
            raise

    async def _drain_until_idle(self) -> None:
        while True:
            self._wake = False
            try:
                await self._drain()
            except Exception:
                log.exception("async_drain_failed")
                self._metrics.drain_failure()
            if not self._wake:
                return

    async def _drain(self) -> None:
        async with self._lock:
            while True:
                now = self._clock.now()
                async with self._uow_factory() as uow:
                    batch = await uow.outbox.claim(
                        limit=50,
                        now=now,
                        reclaim_before=now - self._claim_timeout,
                        token=str(uuid4()),
                    )
                    await uow.commit()
                if not batch:
                    return
                for message in batch:
                    token = message.claim_token or ""
                    try:
                        event = load_event(dict(message.payload))
                    except Exception as exc:
                        log.exception("outbox_decode_failed id=%s", message.id)
                        await self._quarantine(message, exc)
                        continue
                    await self._dispatch(event)
                    await self._finish_claim(message.id, token)

    async def _finish_claim(self, message_id: int, claim_token: str) -> None:
        try:
            async with self._uow_factory() as uow:
                await uow.outbox.mark_published(
                    message_id,
                    when=self._clock.now(),
                    claim_token=claim_token,
                )
                await uow.commit()
        except Exception:
            log.exception("outbox_mark_published_failed id=%s", message_id)
            self._metrics.drain_failure()

    async def _quarantine(self, message: OutboxMessage, exc: Exception) -> None:
        payload = dict(message.payload)
        event_id = str(payload.get("event_id") or message.id)
        event_type = str(payload.get("event_type") or "Unknown")
        token = message.claim_token or ""
        try:
            async with self._uow_factory() as uow:
                await uow.dead_letters.add(
                    DeadLetter(
                        id=str(uuid4()),
                        event_id=event_id,
                        event_type=event_type,
                        handler="outbox_decode",
                        payload=payload,
                        error=str(exc)[:2000],
                        attempts=1,
                        created_at=self._clock.now(),
                    )
                )
                await uow.outbox.mark_published(
                    message.id,
                    when=self._clock.now(),
                    claim_token=token,
                )
                await uow.commit()
        except Exception:
            log.exception("outbox_quarantine_failed id=%s", message.id)
            self._metrics.drain_failure()
            return
        self._metrics.dead_letter("outbox_decode")

    async def _dispatch(self, event: DomainEvent) -> None:
        handlers = list(self._handlers.get(event.event_type, []))
        if not handlers:
            log.info("event_has_no_handlers type=%s id=%s", event.event_type, event.event_id)
            return
        await asyncio.gather(*(self._run_one(handler, event) for handler in handlers))

    async def _run_one(self, handler: object, event: DomainEvent) -> None:
        name = handler_name(handler)
        structlog.contextvars.bind_contextvars(
            event_id=event.event_id,
            event_type=event.event_type,
            handler=name,
        )
        try:
            for attempt in range(1, self._max_retries + 1):
                try:
                    await handler.handle(event)  # type: ignore[attr-defined]
                    return
                except NonRetryableError as exc:
                    await self._dead_letter(event, name, exc, attempt)
                    return
                except Exception as exc:
                    log.warning(
                        "handler_failed handler=%s attempt=%s error=%s",
                        name,
                        attempt,
                        exc,
                    )
                    if attempt >= self._max_retries:
                        await self._dead_letter(event, name, exc, attempt)
                        return
                    self._metrics.handler_retry(name)
                    delay = self._backoff * (2 ** (attempt - 1))
                    if delay > 0:
                        await asyncio.sleep(delay)
        finally:
            structlog.contextvars.unbind_contextvars("event_id", "event_type", "handler")

    async def _dead_letter(
        self,
        event: DomainEvent,
        handler_name: str,
        exc: BaseException,
        attempts: int,
    ) -> None:
        async with self._uow_factory() as uow:
            await uow.dead_letters.add(
                DeadLetter(
                    id=str(uuid4()),
                    event_id=event.event_id,
                    event_type=event.event_type,
                    handler=handler_name,
                    payload=dump_event(event),
                    error=str(exc)[:2000],
                    attempts=attempts,
                    created_at=self._clock.now(),
                )
            )
            await uow.commit()
        self._metrics.dead_letter(handler_name)
        if isinstance(exc, CostLimitError) and handler_name == "listing_rewrite":
            self._metrics.rewrite("cost_limited")
        elif handler_name == "listing_rewrite":
            self._metrics.rewrite("failed")
        elif handler_name == "email_draft":
            self._metrics.email("failed")
        log.error(
            "event_dead_lettered handler=%s id=%s error=%s", handler_name, event.event_id, exc
        )
