"""Decide which queued email drafts a downstream mailer may take. Nothing is sent.

The handoff worker reads queued drafts that have no decision yet, oldest first,
and records exactly one decision per draft in email_handoffs:

1. expired     the draft is older than the max age. No recipients are copied.
2. suppressed  no recipient is left after removing suppressed customer ids,
               or the draft had no recipients to begin with.
3. handed_off  at least one recipient is left. The payload is the mailer copy.

The handoff row is the idempotency record. A second run, or a second process
that lost the race, skips the draft and never changes the first decision. The
email draft itself stays `queued`.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from catalogguard.application.ports import Clock, Metrics, UnitOfWorkFactory
from catalogguard.domain.errors import ConflictError
from catalogguard.domain.models import EmailDraft, EmailHandoff, EmailSuppression

log = logging.getLogger(__name__)

HANDED_OFF = "handed_off"
SUPPRESSED = "suppressed"
EXPIRED = "expired"

_HANDED_OFF_NOTE = (
    "Prepared for a downstream mailer. CatalogGuard did not send this email. "
    "Read stock again before sending; quantity_remaining is as of quantity_as_of."
)
_ALL_SUPPRESSED_NOTE = (
    "Every recipient is on the suppression list. There is nobody to mail. Nothing was sent."
)
_NO_RECIPIENTS_NOTE = "The draft had no recipients. There is nobody to mail. Nothing was sent."
_EXPIRED_NOTE = "The draft aged out before handoff and was not given to a mailer. Nothing was sent."


@dataclass(frozen=True)
class HandoffDecision:
    status: str
    payload: dict[str, object]
    suppressed_customer_ids: list[str]
    remaining_recipients: int


@dataclass(frozen=True)
class HandoffBatchResult:
    examined: int
    handed_off: int
    suppressed: int
    expired: int
    skipped: int


def is_expired(created_at: datetime, *, now: datetime, max_age: timedelta) -> bool:
    return now - created_at > max_age


def draft_recipients(payload: dict[str, object]) -> list[dict[str, object]]:
    """Recipient entries that carry a customer id. Anything else cannot be mailed."""

    raw = payload.get("recipients")
    if not isinstance(raw, list):
        return []
    return [
        dict(item)
        for item in raw
        if isinstance(item, dict) and isinstance(item.get("customer_id"), str)
    ]


def decide_handoff(
    draft: EmailDraft,
    *,
    suppressed: set[str],
    now: datetime,
    max_age_hours: int,
) -> HandoffDecision:
    """Pure decision. `suppressed` is the subset of recipient ids on the suppression list."""

    if is_expired(draft.created_at, now=now, max_age=timedelta(hours=max_age_hours)):
        return HandoffDecision(
            status=EXPIRED,
            payload={
                "handoff_note": _EXPIRED_NOTE,
                "email_draft_id": draft.id,
                "sku": draft.sku,
                "store_id": draft.store_id,
                "draft_created_at": draft.created_at.isoformat(),
                "max_age_hours": max_age_hours,
            },
            suppressed_customer_ids=[],
            remaining_recipients=0,
        )

    recipients = draft_recipients(draft.payload)
    remaining = [row for row in recipients if row["customer_id"] not in suppressed]
    removed: list[str] = []
    for row in recipients:
        customer_id = str(row["customer_id"])
        if customer_id in suppressed and customer_id not in removed:
            removed.append(customer_id)

    payload = dict(draft.payload)
    payload["email_draft_id"] = draft.id
    payload["recipients"] = remaining
    if remaining:
        status = HANDED_OFF
        payload["handoff_note"] = _HANDED_OFF_NOTE
    else:
        status = SUPPRESSED
        payload["handoff_note"] = _ALL_SUPPRESSED_NOTE if recipients else _NO_RECIPIENTS_NOTE
    return HandoffDecision(
        status=status,
        payload=payload,
        suppressed_customer_ids=removed,
        remaining_recipients=len(remaining),
    )


class EmailHandoffService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        metrics: Metrics,
        clock: Clock,
        batch_size: int,
        max_age_hours: int,
    ) -> None:
        if batch_size < 1:
            raise ValueError("email handoff batch size must be at least 1")
        if max_age_hours < 0:
            raise ValueError("email handoff max age must not be negative")
        self._uow_factory = uow_factory
        self._metrics = metrics
        self._clock = clock
        self._batch_size = batch_size
        self._max_age_hours = max_age_hours

    async def run_batch(self) -> HandoffBatchResult:
        """Decide one batch of pending drafts. Not a daemon: call it again to continue."""

        async with self._uow_factory() as uow:
            drafts = await uow.handoffs.pending_drafts(limit=self._batch_size)
        counts = {HANDED_OFF: 0, SUPPRESSED: 0, EXPIRED: 0}
        skipped = 0
        for draft in drafts:
            handoff = await self.record(draft)
            if handoff is None:
                skipped += 1
            else:
                counts[handoff.status] += 1
        result = HandoffBatchResult(
            examined=len(drafts),
            handed_off=counts[HANDED_OFF],
            suppressed=counts[SUPPRESSED],
            expired=counts[EXPIRED],
            skipped=skipped,
        )
        log.info(
            "email_handoff_batch_finished examined=%s handed_off=%s suppressed=%s "
            "expired=%s skipped=%s",
            result.examined,
            result.handed_off,
            result.suppressed,
            result.expired,
            result.skipped,
        )
        return result

    async def record(self, draft: EmailDraft) -> EmailHandoff | None:
        """Record one decision. Returns None when another run already decided this draft."""

        now = self._clock.now()
        max_age = timedelta(hours=self._max_age_hours)
        try:
            async with self._uow_factory() as uow:
                suppressed: set[str] = set()
                if not is_expired(draft.created_at, now=now, max_age=max_age):
                    ids = [str(row["customer_id"]) for row in draft_recipients(draft.payload)]
                    if ids:
                        suppressed = await uow.suppressions.matching(ids)
                decision = decide_handoff(
                    draft,
                    suppressed=suppressed,
                    now=now,
                    max_age_hours=self._max_age_hours,
                )
                handoff = EmailHandoff(
                    id=str(uuid4()),
                    email_draft_id=draft.id,
                    status=decision.status,
                    payload=decision.payload,
                    suppressed_customer_ids=decision.suppressed_customer_ids,
                    created_at=now,
                )
                await uow.handoffs.add(handoff)
                await uow.commit()
        except ConflictError:
            log.info("email_handoff_skipped draft=%s reason=already_decided", draft.id)
            return None
        self._metrics.handoff(handoff.status)
        log.info(
            "email_handoff_recorded draft=%s sku=%s store=%s status=%s recipients=%s suppressed=%s",
            draft.id,
            draft.sku,
            draft.store_id,
            handoff.status,
            decision.remaining_recipients,
            len(decision.suppressed_customer_ids),
        )
        return handoff

    async def list(
        self,
        *,
        status: str | None,
        sku: str | None,
        store_id: str | None,
        limit: int,
        offset: int,
    ) -> list[EmailHandoff]:
        async with self._uow_factory() as uow:
            return await uow.handoffs.list(
                status=status,
                sku=sku,
                store_id=store_id,
                limit=limit,
                offset=offset,
            )


class EmailSuppressionService:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def create(self, *, customer_id: str, reason: str) -> EmailSuppression:
        suppression = EmailSuppression(
            customer_id=customer_id,
            reason=reason,
            created_at=self._clock.now(),
        )
        async with self._uow_factory() as uow:
            if await uow.suppressions.get(customer_id) is not None:
                raise ConflictError("That customer id is already suppressed")
            await uow.suppressions.add(suppression)
            await uow.commit()
        log.info("email_suppression_added")
        return suppression

    async def list(self, *, limit: int, offset: int) -> list[EmailSuppression]:
        async with self._uow_factory() as uow:
            return await uow.suppressions.list(limit=limit, offset=offset)
