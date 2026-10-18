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

Before the handler runs, the attempt commits a row in dead_letter_redrive_claims
in its own short transaction. That row is what another process sees: while it
is younger than the claim timeout, a second request gets 409 and the handler is
not called. An older claim is taken over with a compare-and-set on claimed_at.
The success or failure row is written in the same transaction that releases the
claim. While the handler runs, a keeper refreshes claimed_at so a slow call
does not look idle. A process that dies mid-handler leaves the claim until the
timeout, and a later request can take it over.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
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
IN_PROGRESS = "in_progress"
_ERROR_LIMIT = 2000


class _OwnedClaim:
    """The claimed_at this attempt last wrote. The keeper moves it forward."""

    def __init__(self, claimed_at: datetime) -> None:
        self.claimed_at = claimed_at


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
        claim_timeout_seconds: int,
        claim_renew_seconds: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._handlers = handlers
        self._bus = bus
        self._decode_event = decode_event
        self._metrics = metrics
        self._clock = clock
        self._claim_timeout = timedelta(seconds=claim_timeout_seconds)
        self._renew_seconds = claim_renew_seconds
        self._sleep = asyncio.sleep
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

        claimed_at = await self._claim(letter.id)
        if claimed_at is None:
            self._outcome(dead_letter_id, letter, IN_PROGRESS)
            raise ConflictError("A redrive of this dead letter is already in progress")

        owned = _OwnedClaim(claimed_at)
        keeper = asyncio.create_task(self._keep_claim(letter.id, owned))
        try:
            await handler.handle(event)
        except Exception as exc:
            failed: Exception | None = exc
        else:
            failed = None
        finally:
            keeper.cancel()
            await asyncio.gather(keeper, return_exceptions=True)
        if failed is not None:
            # CostLimitError, NotFoundError, MalformedLLMOutputError, and anything
            # else: record the attempt, then let the existing HTTP mapping answer.
            message = str(failed) or failed.__class__.__name__
            await self._record(letter.id, FAILED, message[:_ERROR_LIMIT], owned.claimed_at)
            self._outcome(dead_letter_id, letter, FAILED)
            raise failed
        redrive = await self._record(letter.id, SUCCEEDED, "", owned.claimed_at)
        self._outcome(dead_letter_id, letter, SUCCEEDED)
        await self._drain_quietly()
        return RedriveResult(
            dead_letter_id=letter.id,
            handler=letter.handler,
            result=SUCCEEDED,
            replayed=False,
            redrive_id=redrive.id,
        )

    async def _claim(self, dead_letter_id: str) -> datetime | None:
        """Commit a claim and return its claimed_at, or None when another one is live."""

        now = self._clock.now()
        async with self._uow_factory() as uow:
            if not await uow.redrive_claims.add(dead_letter_id, now):
                held_since = await uow.redrive_claims.claimed_at(dead_letter_id)
                # A claim that vanished between the insert and this read was just
                # released by its owner; treat it as still busy rather than guess.
                if held_since is None or now - held_since < self._claim_timeout:
                    return None
                if not await uow.redrive_claims.take_over(
                    dead_letter_id, expected=held_since, now=now
                ):
                    return None
            await uow.commit()
        return now

    async def _keep_claim(self, dead_letter_id: str, owned: _OwnedClaim) -> None:
        """Refresh claimed_at until cancelled. Stop if another attempt took the row."""

        while True:
            await self._sleep(self._renew_seconds)
            now = self._clock.now()
            try:
                async with self._uow_factory() as uow:
                    still_ours = await uow.redrive_claims.renew(
                        dead_letter_id, expected=owned.claimed_at, now=now
                    )
                    await uow.commit()
            except Exception:
                log.exception("redrive_claim_renew_failed letter=%s", dead_letter_id)
                continue
            if not still_ours:
                log.warning("redrive_claim_lost letter=%s", dead_letter_id)
                return
            owned.claimed_at = now

    async def _record(
        self, dead_letter_id: str, status: str, error: str, claimed_at: datetime
    ) -> DeadLetterRedrive:
        redrive = DeadLetterRedrive(
            id=str(uuid4()),
            dead_letter_id=dead_letter_id,
            status=status,
            error=error,
            created_at=self._clock.now(),
        )
        async with self._uow_factory() as uow:
            await uow.redrives.add(redrive)
            # Only our own claim. If it timed out and was taken over, the new owner
            # still holds it and releases it when its own attempt finishes.
            await uow.redrive_claims.release(dead_letter_id, claimed_at)
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
