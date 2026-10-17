"""Prometheus metrics. One process-wide registry so app rebuilds do not clash."""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

EVENTS = Counter(
    "catalogguard_events_emitted_total",
    "Domain events accepted into the outbox.",
    ["event_type"],
    registry=REGISTRY,
)
REWRITES = Counter(
    "catalogguard_listing_rewrites_total",
    "Listing rewrite outcomes. failed includes validation failures and dead letters.",
    ["result"],
    registry=REGISTRY,
)
EMAILS = Counter(
    "catalogguard_email_drafts_total",
    "Email draft outcomes. queued means stored for a downstream mailer.",
    ["result"],
    registry=REGISTRY,
)
HANDOFFS = Counter(
    "catalogguard_email_handoffs_total",
    "Email handoff decisions recorded for a downstream mailer. Nothing is sent.",
    ["result"],
    registry=REGISTRY,
)
REDRIVES = Counter(
    "catalogguard_dead_letter_redrives_total",
    "Dead-letter redrive requests by outcome: succeeded, failed, rejected, replayed, in_progress.",
    ["result"],
    registry=REGISTRY,
)
DEAD_LETTERS = Counter(
    "catalogguard_dead_letters_total",
    "Events written to the dead-letter table.",
    ["handler"],
    registry=REGISTRY,
)
DRAIN_FAILURES = Counter(
    "catalogguard_drain_failures_total",
    "Outbox drain failures that were caught after a write had committed.",
    registry=REGISTRY,
)
LEASE_LOST = Counter(
    "catalogguard_outbox_lease_lost_total",
    "Outbox batches whose lease another worker took before this worker finished.",
    registry=REGISTRY,
)
OUTBOX_UNPUBLISHED = Gauge(
    "catalogguard_outbox_unpublished",
    "Unpublished outbox rows, as of the last drain or summary read in this process.",
    registry=REGISTRY,
)
OUTBOX_OLDEST = Gauge(
    "catalogguard_outbox_oldest_unpublished_seconds",
    "Age in whole seconds of the oldest unpublished outbox row, 0 when there is none. "
    "As of the last drain or summary read in this process.",
    registry=REGISTRY,
)
HANDLER_RETRIES = Counter(
    "catalogguard_handler_retries_total",
    "Handler attempts that failed and will be tried again.",
    ["handler"],
    registry=REGISTRY,
)
LLM_LATENCY = Histogram(
    "catalogguard_llm_latency_seconds",
    "Latency of each LLM completion attempt, including the stub provider.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    registry=REGISTRY,
)


class PrometheusMetrics:
    def event_emitted(self, event_type: str) -> None:
        EVENTS.labels(event_type=event_type).inc()

    def rewrite(self, result: str) -> None:
        REWRITES.labels(result=result).inc()

    def email(self, result: str) -> None:
        EMAILS.labels(result=result).inc()

    def handoff(self, result: str) -> None:
        HANDOFFS.labels(result=result).inc()

    def redrive(self, result: str) -> None:
        REDRIVES.labels(result=result).inc()

    def dead_letter(self, handler: str) -> None:
        DEAD_LETTERS.labels(handler=handler).inc()

    def drain_failure(self) -> None:
        DRAIN_FAILURES.inc()

    def outbox_lease_lost(self) -> None:
        LEASE_LOST.inc()

    def outbox_backlog(self, unpublished: int, oldest_age_seconds: int | None) -> None:
        OUTBOX_UNPUBLISHED.set(unpublished)
        OUTBOX_OLDEST.set(0 if oldest_age_seconds is None else oldest_age_seconds)

    def handler_retry(self, handler: str) -> None:
        HANDLER_RETRIES.labels(handler=handler).inc()

    def observe_llm(self, seconds: float) -> None:
        LLM_LATENCY.observe(seconds)


def render_metrics() -> bytes:
    return generate_latest(REGISTRY)


METRICS_CONTENT_TYPE = CONTENT_TYPE_LATEST
