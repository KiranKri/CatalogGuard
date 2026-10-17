"""Read models that are not owned by a write use case."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from catalogguard.application.ports import Clock, Metrics, OutboxStats, UnitOfWorkFactory
from catalogguard.domain.models import DeadLetter, DeadLetterRedrive


@dataclass(frozen=True)
class OutboxBacklog:
    unpublished: int
    claimed: int
    oldest_unpublished_age_seconds: int | None


def outbox_backlog(stats: OutboxStats, *, now: datetime) -> OutboxBacklog:
    """Whole seconds, truncated toward zero, never negative. None when nothing is pending."""

    age: int | None = None
    if stats.oldest_created_at is not None:
        age = max(0, int((now - stats.oldest_created_at).total_seconds()))
    return OutboxBacklog(
        unpublished=stats.unpublished,
        claimed=stats.claimed,
        oldest_unpublished_age_seconds=age,
    )


class OutboxSummaryService:
    """Backlog read from the database, so it does not depend on which process drained last."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        clock: Clock,
        metrics: Metrics,
        claim_timeout_seconds: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._metrics = metrics
        self._claim_timeout = timedelta(seconds=claim_timeout_seconds)

    async def summary(self) -> OutboxBacklog:
        now = self._clock.now()
        async with self._uow_factory() as uow:
            stats = await uow.outbox.stats(claimed_since=now - self._claim_timeout)
        backlog = outbox_backlog(stats, now=now)
        self._metrics.outbox_backlog(backlog.unpublished, backlog.oldest_unpublished_age_seconds)
        return backlog


@dataclass(frozen=True)
class DeadLetterView:
    letter: DeadLetter
    redrive_count: int
    last_redrive_status: str | None
    last_redriven_at: datetime | None


class DeadLetterService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def list_with_redrives(self, *, limit: int, offset: int) -> list[DeadLetterView]:
        """Letters plus their redrive history. "Last" is newest created_at, then greatest id."""

        async with self._uow_factory() as uow:
            letters = await uow.dead_letters.list(limit=limit, offset=offset)
            attempts = await uow.redrives.for_letters([letter.id for letter in letters])
        by_letter: dict[str, list[DeadLetterRedrive]] = {}
        for attempt in attempts:
            by_letter.setdefault(attempt.dead_letter_id, []).append(attempt)
        views: list[DeadLetterView] = []
        for letter in letters:
            rows = by_letter.get(letter.id, [])
            last = max(rows, key=lambda row: (row.created_at, row.id)) if rows else None
            views.append(
                DeadLetterView(
                    letter=letter,
                    redrive_count=len(rows),
                    last_redrive_status=None if last is None else last.status,
                    last_redriven_at=None if last is None else last.created_at,
                )
            )
        return views

    async def list(self, *, limit: int, offset: int) -> list[DeadLetter]:
        async with self._uow_factory() as uow:
            return await uow.dead_letters.list(limit=limit, offset=offset)
