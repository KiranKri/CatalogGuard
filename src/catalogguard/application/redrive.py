"""Rerun one dead letter's handler on operator request. Nothing is emailed.

A redrive decodes the stored event and calls `handle` once on the one
subscribed handler whose name is on the letter. It does not go back through
the outbox, so other subscribers do not run again. It does not retry and it
never writes a new dead letter. The letter itself is kept.

Each attempt is a row in dead_letter_redrives. Any `succeeded` row is the
idempotency record: a later request replays it without calling the handler.
A `failed` row does not block another attempt. Handlers are already safe to
run again for the same event id, so a redrive reuses their own idempotency
(processed_events, the listing trigger id, and the email dedupe key) and the
same daily LLM cap path.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4
from weakref import WeakValueDictionary

from catalogguard.application.ports import (
    Clock,
    EventBus,
    HandlerRegistry,
    Metrics,
    UnitOfWorkFactory,
)
from catalogguard.domain.errors import ConflictError, NotFoundError
from catalogguard.domain.events import DomainEvent
from catalogguard.domain.models import DeadLetter, DeadLetterRedrive

log = logging.getLogger(__name__)

OUTBOX_DECODE = "outbox_decode"
SUCCEEDED = "succeeded"
FAILED = "failed"
REJECTED = "rejected"
REPLAYED = "replayed"
_ERROR_LIMIT = 2000


@dataclass(frozen=True)
class RedriveResult:
    dead_letter_id: str
    handler: str
    result: str
    replayed: bool
    redrive_id: str


class DeadLetterRedriveService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        handlers: HandlerRegistry,
        bus: EventBus,
        decode_event: Callable[[dict[str, object]], DomainEvent],
        metrics: Metrics,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._handlers = handlers
        self._bus = bus
        self._decode_event = decode_event
        self._metrics = metrics
        self._clock = clock
        # One lock per letter id in this process, so a double click waits and then
        # replays instead of running the handler twice. Entries vanish when unused.
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def redrive(self, dead_letter_id: str) -> RedriveResult:
        lock = self._locks.get(dead_letter_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[dead_letter_id] = lock
        async with lock:
            return await self._redrive(dead_letter_id)

    async def _redrive(self, dead_letter_id: str) -> RedriveResult:
        async with self._uow_factory() as uow:
            letter = await uow.dead_letters.get(dead_letter_id)
            prior = None
            if letter is not None and letter.handler != OUTBOX_DECODE:
                prior = await uow.redrives.first_success(dead_letter_id)

        if letter is None:
            self._outcome(dead_letter_id, None, REJECTED)
            raise NotFoundError(f"Dead letter {dead_letter_id} not found")
        if letter.handler == OUTBOX_DECODE:
            self._outcome(dead_letter_id, letter, REJECTED)
            raise ConflictError("Undecodable outbox rows cannot be redriven")
        if prior is not None:
            self._outcome(dead_letter_id, letter, REPLAYED)
            return RedriveResult(
                dead_letter_id=letter.id,
                handler=letter.handler,
                result=REPLAYED,
                replayed=True,
                redrive_id=prior.id,
            )

        try:
            event = self._decode_event(dict(letter.payload))
        except Exception as exc:
            self._outcome(dead_letter_id, letter, REJECTED)
            raise ConflictError("Dead-letter payload cannot be decoded") from exc
        handler = self._handlers.handler_for(letter.event_type, letter.handler)
        if handler is None:
            self._outcome(dead_letter_id, letter, REJECTED)
            raise ConflictError(
                f"No handler named {letter.handler} is registered for {letter.event_type}"
            )

        try:
            await handler.handle(event)
        except Exception as exc:
            # CostLimitError, NotFoundError, MalformedLLMOutputError, and anything
            # else: record the attempt, then let the existing HTTP mapping answer.
            message = str(exc) or exc.__class__.__name__
            await self._record(letter.id, FAILED, message[:_ERROR_LIMIT])
            self._outcome(dead_letter_id, letter, FAILED)
            raise
        redrive = await self._record(letter.id, SUCCEEDED, "")
        self._outcome(dead_letter_id, letter, SUCCEEDED)
        await self._drain_quietly()
        return RedriveResult(
            dead_letter_id=letter.id,
            handler=letter.handler,
            result=SUCCEEDED,
            replayed=False,
            redrive_id=redrive.id,
        )

    async def _record(self, dead_letter_id: str, status: str, error: str) -> DeadLetterRedrive:
        redrive = DeadLetterRedrive(
            id=str(uuid4()),
            dead_letter_id=dead_letter_id,
            status=status,
            error=error,
            created_at=self._clock.now(),
        )
        async with self._uow_factory() as uow:
            await uow.redrives.add(redrive)
            await uow.commit()
        return redrive

    async def _drain_quietly(self) -> None:
        # A threshold redrive only writes LowInventoryDetected to the outbox, so
        # drain once to run listing and email. The redrive already committed, so a
        # drain failure is logged and not returned as an error. The bus counts its
        # own failures in catalogguard_drain_failures_total.
        try:
            await self._bus.drain()
        except Exception:
            log.exception("drain_failed_after_redrive")

    def _outcome(self, dead_letter_id: str, letter: DeadLetter | None, result: str) -> None:
        self._metrics.redrive(result)
        log.info(
            "dead_letter_redriven letter=%s handler=%s event=%s result=%s",
            dead_letter_id,
            None if letter is None else letter.handler,
            None if letter is None else letter.event_id,
            result,
        )
