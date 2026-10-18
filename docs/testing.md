# Tests

`make test` covers threshold resolution, crossing edges, cooldown, flapping, optimistic concurrency, idempotent inventory updates, event-bus retry and dead letter, outbox leases, listing generation (stub, malformed output, validation failure), the guardrail eval file, publish, the daily LLM cap, an Alembic upgrade, and an API test of the full low-inventory path. `tests/integration/test_email_handoff.py` covers the handoff worker: New York recipients, one suppressed customer, all suppressed, a draft with no recipients, expiry and its boundary, the sold-out skip, oldest-first batching, filters, a second run, a lost race that must not overwrite, duplicate suppressions, and the `python -m catalogguard.mailer` entry point on a fresh database.

`tests/integration/test_dead_letter_redrive.py` covers the redrive endpoint:

- recovering malformed model output, then replaying without calling the handler or the model
- concurrent POSTs
- the daily cap: 429 on the same UTC day, success the next day
- refusing `outbox_decode`, unknown ids, undecodable payloads, and unregistered handlers
- email recovery that keeps one listing draft and one queued email
- threshold recovery that drains into both drafts without a handoff
- a drain failure after a successful redrive
- a failed attempt, truncated to 2000 characters, followed by a successful retry

`tests/integration/test_outbox_lease.py` covers the outbox lease, the drain worker, and the backlog:

- renewal, idle after publish, and a stolen lease that is lost
- the keeper renewing during an in-flight handler
- a lost batch that stops without publishing or dead-lettering
- the async API leaving rows that `python -m catalogguard.drain` publishes
- the inline API still having drafts when the PUT returns
- the drain command on a fresh database
- the summary counts and gauges
- the startup check on the renew interval

`tests/integration/test_listing_revisions.py` covers listing history:

- stripped revisions on publish, and no revision on a repeat publish
- rollback through publish
- rolling back to the live version, with a stale version, or to a draft that no longer matches the product
- the 404 cases

`tests/integration/test_inventory_changes.py` covers inventory history:

- one row per real change (create, quantity, threshold set, threshold cleared), with nothing added for no-op writes, replays, a stale version, or an unknown store
- matching `event_id` and `occurred_at` with the outbox event
- newest-first pagination
- 404s versus an empty page for a pair never stocked
- no history from the seed

`tests/integration/test_low_stock.py` covers the board and the cause link:

- the strict comparison (quantity equal to the threshold is not low)
- latest-draft selection and its id tie-break
- a zero override, and a sold-out row with no email
- all three threshold sources on seed data, with pagination
- the 404s and the empty page
- the cause chain back to the inventory write
- the manual-draft case
- missing or mismatched links

`tests/integration/test_suppressed_crossings.py` covers cooldown suppressions:

- a crossing inside the window stored with its quantities, threshold, and `cooldown_until`, with no new draft and the cooldown unchanged
- a crossing at the exact end of the window that fires and is not stored
- no row for a move that stays low or for a first insert
- a redelivered event, and the unique `inventory_event_id` backstop
- the store and SKU filters, ordering, pagination, and the 404

`make lint` runs Ruff. `.github/workflows/ci.yml` runs three jobs on Ubuntu 24.04 for pushes and pull requests to `main`: Ruff and `pytest -q` on SQLite with the stub LLM, the same suite on Postgres 16 (including the two-engine lock tests), and `scripts/compose_smoke.sh`. GitHub Actions run [39130397208](https://github.com/KiranKri/CatalogGuard/actions/runs/39130397208) passed all three. The Postgres job reported 109 passed and 7 skipped. Those skips are the `sqlite_only` tests. The five `postgres_only` tests passed: Alembic upgrade, two-engine outbox claims, `SKIP LOCKED` while a row lock is held, lease-token publish, and two concurrent redrives. The drain command was not part of that Postgres job or the Compose smoke.

A click-by-click SQLite pass is in [manual-regression.md](manual-regression.md).
