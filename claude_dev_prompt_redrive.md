# Dev prompt: dead-letter redrive

You are continuing development on CatalogGuard, the repository in the current working directory.

The work so far is committed on `main` as `8deb824` (`Add CatalogGuard catalog, listings, and email handoff.`). Do not amend that commit. Do not create another commit. Do not push. Do not add a remote.

This is one feature, done completely, in about 90 minutes. Stop when it is done. Do not start a second feature.

Read the code before editing. If this prompt disagrees with the repository, trust the repository and adapt the design, then say what you changed in the summary.

## Do not undo recent behavior

Leave these in place:

- Drafts keep `[[key]]` markers. `store_listings` text has those markers removed. Publish re-checks the draft against the product's current attributes and returns 409 when they no longer match. Republishing the same draft with the wrong `expected_version` is also 409.
- Each `[[key]]` must have its attribute value in the 48 characters immediately before that marker.
- Quantity 0 does not queue an "act now" email. Other email drafts include `quantity_as_of`. The handoff worker copies `quantity_as_of` through and does not invent an email for a sold-out skip that never created a draft.
- An outbox row that cannot be decoded is dead-lettered as `outbox_decode` and marked published. `mark_published` requires the `claim_token` from the lease. Drain failures after a committed inventory write are logged and do not turn that write into an HTTP 500.
- `LowInventoryDetected.event_id` is a UUID5 of the inventory event id (namespace `6f1d8c2a-4b7e-5d19-8c33-1a2b3c4d5e6f`). `processed_events` conflicts roll the handler transaction back instead of being ignored.
- `X-Correlation-ID` longer than 64 characters is HTTP 400.
- `create_all` runs only for SQLite. Postgres relies on Alembic. Do not call `create_all` for a Postgres URL from the API or the mailer.
- A concurrent retry of the same idempotency key replays the stored response.
- Inline dispatch stays the default. Compose stays inline. The README curl walkthrough must still see drafts when the inventory PUT returns.
- Email drafts stay `status = queued`. There is no send route and no SMTP.
- Handoff decisions are final. `email_handoffs.email_draft_id` is unique. A second handoff run does not change the first decision. Suppressions are exact opaque customer ids.
- `catalogguard_dead_letters_total` already has a `handler` label. Do not remove it and do not add a second dead-letter counter. The review note that said this label was missing is stale.

Do not change threshold rules, cooldown duration, listing validation, citation rules, the LLM providers, the handoff decision rules, or the prompt text. Do not split the listing and email cooldown. Do not tell the model why a rewrite ran. Do not add a global LLM budget. Do not add Kafka, OpenTelemetry, auth, a UI, or a drain daemon. Do not install Docker and do not spend time on it.

## Outcome

Operators can recover a handler dead letter without writing SQL and without replaying every handler on that event.

`POST /api/v1/dead-letters/{id}/redrive` runs only the named handler with the stored event. The dead-letter row stays. A successful redrive is recorded and a second POST does not run the handler again. A failed redrive is recorded and a later POST may try again. Nothing is emailed.

When you finish:

- `make test` passes, or `.venv\Scripts\python.exe -m pytest -q` if `make` is not on PATH
- `make lint` passes, or `ruff check src tests` and `ruff format --check src tests`
- README describes the endpoint, the refusal for `outbox_decode`, and that a redrive does not send email
- ORM and a **new** Alembic revision both describe the new table. `down_revision` is `15e685dc71f6`. Do not edit `migrations/versions/0d865ba6ff4d_initial_schema.py` or `migrations/versions/15e685dc71f6_email_handoff.py`
- Local `catalogguard.db`, `catalogguard.db-wal`, and `catalogguard.db-shm` are deleted if present, then `make seed` is run so the hero SKU is quantity 12, version 1
- `DEV_NOTES.md` keeps the email-handoff notes and gains a new section for this feature: what you built, how you tested, and what you deliberately did not do
- You leave no server running and you do not commit

## Context you need

### What a dead letter is today

`GET /api/v1/dead-letters` lists rows. There is no redrive.

Two writers:

- `InProcessEventBus._dead_letter` stores `dump_event(event)` after a handler raises `NonRetryableError`, or after `HANDLER_MAX_RETRIES` other failures. The outbox row is still marked published, so the drain moves on. Handlers in this path are `threshold_evaluation` (`InventoryChanged`), `listing_rewrite` (`LowInventoryDetected`), and `email_draft` (`LowInventoryDetected`).
- `InProcessEventBus._quarantine` stores the raw outbox dict with `handler = outbox_decode` when `load_event` fails, then `mark_published` with the lease token. That payload is not a domain event.

`catalogguard_dead_letters_total{handler}` increments once when the letter is written.

Listing validation failures are stored on `listing_drafts` with status `validation_failed`. They are not dead letters. A manual rewrite over `LLM_DAILY_CAP_PER_SKU` returns 429 and does not write a dead letter. The low-stock listing handler does write a dead letter on `CostLimitError`, and the email handler still queues its draft.

Handlers are already safe to run again:

- `ThresholdHandler` marks `processed_events` first. A second call for an event that committed returns without publishing another `LowInventoryDetected` and without moving the cooldown.
- `ListingService.generate` returns the existing draft for that `trigger_event_id` before it reserves an LLM call.
- `EmailDraftService.generate` returns the existing row for `low-stock:{sku}:{store_id}:{event_id}`. Quantity 0 returns `None` and inserts nothing.

The cap is consumed inside `generate`, before the provider call, per SKU per UTC date from the injected clock. A redrive must use that same path. Do not add a bypass that skips the cap.

`publish` on the bus only inserts an outbox row. The only drains today are inventory upsert (after commit) and app startup. A threshold redrive that publishes `LowInventoryDetected` will not run listing and email unless the redrive calls `drain()` afterward. Inline `drain()` waits. Async `drain()` schedules the existing loop. Call it once after a successful handler return. If that drain raises, log it, increment `catalogguard_drain_failures_total`, and still return success for the redrive. Do not write another dead letter from that drain failure. This matches the inventory rule that a committed write is not turned into an HTTP 500.

Do not redrive by inserting the event back onto the outbox. That would run every subscriber, not the one that failed, and it would re-quarantine an `outbox_decode` row.

### Layout

```
src/catalogguard/domain/          no FastAPI, SQLAlchemy, or Pydantic
src/catalogguard/application/     use cases and ports
src/catalogguard/infrastructure/  SQLAlchemy, bus, LLM, HTTP
prompts/  evals/  migrations/  tests/  adr/
```

Match the existing style. Use the injected `Clock`, not `datetime.now`, in application code. Logs are structured JSON via stdlib loggers. Do not log the dead-letter payload, recipient ids, or customer ids. Metrics use the registry in `infrastructure/metrics.py`. Domain errors already map to HTTP in `infrastructure/api/app.py`. `CostLimitError` is 429 `llm_cost_limit`. `NotFoundError` is 404. `ConflictError` is 409 `conflict`. `MalformedLLMOutputError` is 502 `malformed_llm_output`.

Tests use `pytest-asyncio`, the `harness` fixture in `tests/conftest.py`, and helpers in `tests/support.py`. The harness builds a fresh SQLite file and calls `create_all`. It does not run Alembic. New tables must be on the SQLAlchemy metadata. `tests/integration/test_migrations.py` upgrades an empty SQLite file twice. Extend it so the new table exists and `alembic_version` is the new revision.

Ruff: line length 100, select E, F, I, UP, B. `make` may be absent. The Makefile uses `.venv/Scripts/python.exe` on Windows when that file exists.

Head migration today: `15e685dc71f6` revises `0d865ba6ff4d`.

## Feature

### Table `dead_letter_redrives`

- `id` string primary key
- `dead_letter_id` string, required, foreign key to `dead_letters.id`
- `status` string, check constraint: `succeeded` or `failed`
- `error` string, required, empty when the status is `succeeded`, otherwise a short message (truncate to 2000 characters)
- `created_at` timezone-aware datetime
- index on `dead_letter_id`

More than one row per letter is allowed. Failures can be followed by another attempt. The application treats "any `succeeded` row for this letter" as the idempotency record. Do not overwrite a failed row into a success. Insert a new row.

Implement `downgrade` by dropping the new table. ORM check constraint, foreign key, and index names must match the migration.

### Decision rules

`POST /api/v1/dead-letters/{id}/redrive` takes no body. Ignore a body if one is sent. Do not accept a replacement payload.

1. Unknown id: 404 `not_found`. No redrive row.
2. `handler` is `outbox_decode`: 409 `conflict`, message `Undecodable outbox rows cannot be redriven`. Do not call `load_event`. Do not insert a redrive row. Do not touch the outbox.
3. A `succeeded` redrive already exists: return 200 with `result` `replayed` and `replayed` true. Do not call the handler. Do not drain. Do not insert another row. `redrive_id` is the existing success row's id.
4. Otherwise `load_event` on the stored payload. If that raises, 409 `conflict`, message `Dead-letter payload cannot be decoded`. No redrive row. No new dead letter.
5. Look up the single registered handler whose name equals `letter.handler` for `letter.event_type`. Add a public lookup on `InProcessEventBus`. Do not construct a second handler instance in the route. If none is registered, 409 `conflict`, message `No handler named {name} is registered for {event_type}`. No redrive row.
6. Call that handler's `handle(event)` once. Do not use `_run_one`. Do not retry. Do not write a new dead letter from this call, whether it succeeds or fails.
7. On success, insert a `succeeded` redrive row and commit it, then `drain()` once under the rule above, then return 200:

```json
{
  "dead_letter_id": "...",
  "handler": "listing_rewrite",
  "result": "succeeded",
  "replayed": false,
  "redrive_id": "..."
}
```

8. On `CostLimitError`, commit a `failed` redrive row whose error is the exception message, then let the existing 429 mapping run. The dead-letter count does not increase.
9. On `NotFoundError`, commit a `failed` row, then let the existing 404 mapping run.
10. On `MalformedLLMOutputError`, commit a `failed` row, then let the existing 502 mapping run.
11. On any other exception, commit a `failed` row with the exception message truncated to 2000 characters, then propagate. Do not catch it into a new dead letter.

A failed row does not block the next POST. Only a succeeded row does.

Do not delete or edit the dead letter. Do not clear or extend `low_stock_cooldowns`. Do not change `email_drafts.status`. Do not create an `email_handoffs` row. Quantity 0 inside a recovered `LowInventoryDetected` still creates no email draft, because `generate` already returns `None`.

`GET /api/v1/dead-letters` keeps its pagination and adds these fields on each item:

- `redrive_count` (integer, 0 when none)
- `last_redrive_status` (`succeeded`, `failed`, or null)
- `last_redriven_at` (ISO timestamp or null)

"Last" is the newest `created_at`, then the greatest `id`.

### Metrics and logs

Add `catalogguard_dead_letter_redrives_total{result}` with results `succeeded`, `failed`, `rejected`, and `replayed`. Increment once per POST outcome. `rejected` covers the 409 cases that do not insert a row. Do not increment `catalogguard_dead_letters_total` during redrive.

Log `dead_letter_redriven` with the letter id, handler, event id, and result. Do not log the payload.

### Tests

Add tests under `tests/`. Use the harness. Do not weaken existing assertions. Keep the suite fast. No sleeps. Use `FakeClock.advance` when a test needs another UTC day.

Cover at least:

- Malformed listing output dead-letters `listing_rewrite` and still queues one email, which is existing behavior. Change the same `FixedLLM` instance to valid JSON (a setter on `FixedLLM` is fine; `ListingService` already holds that object). `tests/unit/test_listing_validation.py` `_valid()` matches `tests/support.py` `create_product` (`color` olive). POST redrive. Exactly one listing draft exists and its status is `ready`. The email draft count stays 1 and its status stays `queued`. The dead letter remains. `last_redrive_status` is `succeeded`. A second POST returns `replayed` true, does not call the handler, and does not call the LLM again.
- With `llm_daily_cap_per_sku=1`, the manual rewrite takes the slot and the low-stock listing handler dead-letters, which is existing behavior. POST redrive on that same UTC day returns 429 `llm_cost_limit`. The dead-letter list still has one letter. `last_redrive_status` is `failed`. No second listing draft. Advance the clock one day. POST again. That call succeeds, a listing draft for the event's trigger id exists, and the dead-letter count is still 1.
- `outbox_decode` redrive returns 409 `conflict` with the undecodable message. The letter remains. `redrive_count` stays 0. Build the poison row the way `tests/unit/test_outbox_poison.py` does, or POST the letter that test already produces.
- Unknown id returns 404.
- Email recovery: on the harness app, replace the subscribed `email_draft` handler's `handle` with a function that raises `RuntimeError` before the crossing PUT, so the listing draft is stored and the email handler dead-letters. Restore the original `handle`. POST redrive. Exactly one email draft exists, status `queued`, and the listing draft count stays 1. A second POST is `replayed` and does not call `handle`.
- Threshold recovery: insert one `dead_letters` row, through the repository, whose payload is `dump_event` of an `InventoryChanged` with previous quantity 10, new quantity 2, a fresh event id, and `occurred_at` from the harness clock. Handler name `threshold_evaluation`. Do not change `ThresholdHandler` to read the current stock quantity. The crossing uses the event quantities. POST redrive. Because drain runs before the response, one listing draft and one queued email draft exist. A second POST is `replayed` and the draft counts stay the same. No `email_handoffs` row is created.
- Alembic upgrade on an empty database creates `dead_letter_redrives` and the head revision is the new one. The previous head remains `15e685dc71f6` as this revision's parent.

`FixedLLM.calls` is the LLM counter. For the replay test, wrap or spy on `handle` if that is the clearest way to prove it was not called.

### Docs

Update the failure-modes paragraph and the API table in `README.md`. State:

- `POST /api/v1/dead-letters/{id}/redrive` runs only that letter's handler
- the letter is kept
- a second POST after success does not run the handler again
- a failure can be retried
- `outbox_decode` returns 409 and is not put back on the outbox
- a recovered email draft stays `queued`
- nothing is sent
- a listing redrive spends the daily cap when no draft exists yet for that trigger, and spends nothing when the draft already exists

Do not put a poison row into the main curl walkthrough. One short example with a placeholder id is enough, using `curl.exe` in the same PowerShell style as the rest of the README.

Append to `DEV_NOTES.md`. Do not remove the handoff section.

## Done when

Tests and lint are green. `DEV_NOTES.md` describes this feature and still describes handoff. The hero seed is quantity 12, version 1. No server is running. No commit was made.
