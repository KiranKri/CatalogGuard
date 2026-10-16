# Dev prompt

You are continuing development on CatalogGuard, the repository in the current working directory.

## Do not undo recent review fixes

A review pass just landed. Leave these behaviors in place:

- Drafts keep `[[key]]` markers. `store_listings` text has those markers removed. Publish re-checks the draft against the product's current attributes and returns 409 when they no longer match. Republishing the same draft with the wrong `expected_version` is also 409.
- Each `[[key]]` must have its attribute value in the 48 characters immediately before that marker.
- Quantity 0 does not queue an "act now" email. Other email drafts include `quantity_as_of`. The handoff worker must copy `quantity_as_of` through when it copies a payload, and it must not invent an email for a sold-out skip that never created a draft.
- An outbox row that cannot be decoded is dead-lettered as `outbox_decode` and marked published. `mark_published` requires the `claim_token` from the lease. Drain failures after a committed inventory write are logged and do not turn that write into an HTTP 500.
- `LowInventoryDetected.event_id` is a UUID5 of the inventory event id. `processed_events` conflicts roll the handler transaction back instead of being ignored.
- `X-Correlation-ID` longer than 64 characters is HTTP 400.
- `create_all` runs only for SQLite. Postgres relies on Alembic.
- A concurrent retry of the same idempotency key replays the stored response.

Do not revert any of that while adding the handoff worker.

You are continuing development on CatalogGuard. A previous pass finished the catalog/inventory service, listing drafts, a reviewed publish step, a per-SKU daily LLM cap, an outbox lease, and an initial Alembic revision. Your job is one follow-on feature, done completely, in about 60 to 90 minutes. Stop when that feature is done. Do not start a second feature.

Read the code before editing. If this prompt disagrees with the repository, trust the repository and adapt the design, then say what you changed in the summary.

## Outcome

Add a downstream email handoff step. CatalogGuard still does not send email. It decides which queued drafts are ready for a mailer, which recipients are suppressed, and which drafts are too old to hand off. A separate process records that decision. The existing low-stock path must keep creating `email_drafts` rows with status `queued`.

When you finish:

- `make test` passes
- `make lint` passes
- README describes the new behavior and a copy-pasteable way to run it after the existing low-stock curl walkthrough
- `.env.example` documents every new setting
- ORM and a **new** Alembic revision both describe the new tables. Do not edit `migrations/versions/0d865ba6ff4d_initial_schema.py`
- Local `catalogguard.db` is recreated with `make seed` if the schema change means the old file would break `make run`
- You leave a short `DEV_NOTES.md` in the repo root: what you built, how you tested, and anything you deliberately did not do

## Context you need

### Product behavior already shipped

Inventory is per (SKU, store). An upsert requires `Idempotency-Key` and `expected_version` (`0` creates the row). Optimistic conflicts return 409 `optimistic_conflict` with `current_version`.

Effective threshold is the (SKU, store) override, else the SKU override, else `DEFAULT_LOW_STOCK_THRESHOLD` (5). Low stock means `quantity < threshold`.

A low-stock event fires only on a downward crossing (previous quantity was at or above the threshold, new quantity is strictly below) and only if that pair is outside `LOW_STOCK_COOLDOWN_SECONDS` (default 24h). A crossing during the cooldown is dropped, not deferred. The first insert is not a crossing.

Flow: inventory commit writes an `InventoryChanged` outbox row in the same transaction. `InProcessEventBus.drain` leases unpublished rows (`claimed_at`, `claim_token`). On Postgres the lease uses `FOR UPDATE SKIP LOCKED`. A lease older than `OUTBOX_CLAIM_TIMEOUT_SECONDS` (default 300) can be taken again. Handlers then run:

- listing rewrite stores a `listing_drafts` row (`ready` or `validation_failed`). It does not change product SEO fields.
- email draft stores one `email_drafts` row, status `queued`, dedupe key `low-stock:{sku}:{store_id}:{event_id}`.

`POST /api/v1/listings/drafts/{id}/publish` with `expected_version` copies a ready draft onto `store_listings`. `validation_failed` cannot be published. Product SEO stays as catalog defaults.

LLM calls go through a port. Default provider is `StubLLM` (`stub-deterministic-v1`), no API key. `LLM_PROVIDER=openai` uses OpenAI-compatible Chat Completions. Default base URL is `https://api.x.ai/v1`, model `grok-4.7`. `LLM_DAILY_CAP_PER_SKU` (default 24, `0` disables) reserves a call per SKU per UTC day before the provider runs. Over the cap, a manual rewrite returns 429 `llm_cost_limit`. The event handler dead-letters and still queues the email draft.

Email targeting is a deliberate simplification. Recipients are opaque `customer_id` values from `recent_interactions` (`view` or `add_to_cart`) for that SKU and store, inside `EMAIL_LOOKBACK_HOURS` (default 168), measured from the event time. One row per customer, preferring `add_to_cart`, then the later timestamp. The body comes from `prompts/email_urgency/v1.md`. Customer ids are not sent to the LLM.

Demo seed (`python -m catalogguard.seed` or `make seed`) loads 10 products, stores `nyc`, `chi`, and `aus`, inventory, and interactions. Hero case: `BKPK-28L-OLV` at `nyc` starts at quantity 12, version 1. Customers `cus_1842` (add_to_cart), `cus_4410`, `cus_7781`, `cus_9021` are in the New York window. `cus_1009` viewed the same SKU in Chicago and must not be on the New York draft.

### Layout and conventions

```
src/catalogguard/domain/          no FastAPI, SQLAlchemy, or Pydantic
src/catalogguard/application/     use cases and ports
src/catalogguard/infrastructure/  SQLAlchemy, bus, LLM, HTTP
prompts/  evals/  migrations/  tests/  adr/
```

Match the existing style: dataclasses in the domain, repository protocols on `application/ports.py`, SQL in `infrastructure/db/repositories.py`, wiring in `infrastructure/container.py`, HTTP in `infrastructure/api/`. Use the injected `Clock`, not `datetime.now`, in application code. Logs are structured JSON via stdlib loggers; do not log recipient ids, log counts. Metrics use the existing Prometheus registry in `infrastructure/metrics.py`. Domain errors already map to HTTP in `infrastructure/api/app.py`.

Tests use `pytest-asyncio`, the `harness` fixture in `tests/conftest.py`, and helpers in `tests/support.py`. The harness builds a fresh SQLite file and calls `create_all`. It does not run Alembic. New tables must be on the SQLAlchemy metadata or tests will not see them.

Local SQLite and tests create tables with `metadata.create_all`. Docker runs `python -m catalogguard.migrate` before uvicorn. A new revision must apply on top of `0d865ba6ff4d`. Do not rewrite that revision. `tests/integration/test_migrations.py` upgrades an empty SQLite file twice. Extend that test, or add another, so the new tables exist after upgrade.

Check constraint today:

```text
email_drafts.status in ('queued')
```

Leave that constraint and the `queued` status alone. Existing tests assert `status == "queued"` and the dedupe key. Do not flip those rows to `sent` or `handed_off`.

Ruff is the linter (`make lint`): line length 100, select E, F, I, UP, B. FastAPI `Depends` / `Query` defaults are ignored for B008 only in `routes.py`.

`make` may be absent from PATH. The Makefile uses `.venv/Scripts/python.exe` on Windows when that file exists. Run that interpreter directly if `make` is missing: `python -m pytest -q` and `python -m ruff check src tests` plus `ruff format --check`.

### What not to build

- No SMTP, no network call, no "send" that could be pointed at a real provider
- No Kafka client, no OpenTelemetry, no auth, no UI
- No change to threshold, cooldown, listing validation, or the LLM providers
- No drive-by refactors
- Do not spend time on Docker. It is not available here

## Feature

### Tables

`email_suppressions`

- `customer_id` string primary key (same opaque id space as `recent_interactions`)
- `reason` string, required, max 200 characters
- `created_at` timezone-aware datetime

`email_handoffs`

- `id` string primary key
- `email_draft_id` string, required, unique, foreign key to `email_drafts.id`
- `status` string, check constraint: `handed_off`, `suppressed`, or `expired`
- `payload` JSON. This is the mailer-facing copy, not a second source of truth for the original draft
- `suppressed_customer_ids` JSON list of strings
- `created_at` timezone-aware datetime
- index on `status`

A handoff row means the worker has made a decision for that draft. Creating it is the idempotency record. A second run must not insert another row and must not change the first decision.

### Decision rules

Process drafts that are still `queued` and have no handoff row. Oldest first. Default batch size 50 (`EMAIL_HANDOFF_BATCH_SIZE`).

Use the clock injected into the worker, not wall time hidden inside the repository.

1. If `now - draft.created_at` is greater than `EMAIL_HANDOFF_MAX_AGE_HOURS` (default 72), insert a handoff with status `expired`. Payload explains that the draft aged out and includes the draft id, sku, and store id. Do not include the recipient list on an expired handoff.
2. Otherwise copy the draft payload. Remove any recipient whose `customer_id` is in `email_suppressions`. Record those ids on `suppressed_customer_ids`.
3. If no recipients remain, status is `suppressed`. The payload keeps the subject and body but `recipients` is empty, plus a short `handoff_note` that every recipient was suppressed. Still no addresses.
4. If at least one recipient remains, status is `handed_off`. The payload's `recipients` are only the remaining people. Preserve the other draft fields (`subject`, `body`, `template_version`, `quantity_remaining`, `sku`, `store_id`, `store_name`, `product_name`, `targeting`, `targeting_note`). Add `handoff_note`: this record is for a downstream mailer and was not sent.

Suppression is exact `customer_id` match. Do not inspect names. There are no names.

Empty original recipient lists follow the same rules: age check first, otherwise status `suppressed` with an empty recipient list. That draft has nobody to mail.

### Worker

`python -m catalogguard.mailer` loads `Settings`, creates the engine, `create_all` is already done by app startup but this command must also call `init_db` so it works on a fresh database, then runs one batch and exits. It is not a long-running daemon. Say that in the README. A later scheduler can call it again.

Race: two processes may select the same draft. Insert the handoff inside a savepoint. On the unique conflict, skip that draft and continue. Do not raise, and do not overwrite the winner's row.

Wire it the same way as the app: settings, clock, unit of work. Do not import FastAPI into the domain or the application service.

### HTTP

- `POST /api/v1/email-suppressions` body `{ "customer_id", "reason" }`. 201 on create. 409 if that customer id already exists. Validate `customer_id` with `^[A-Za-z0-9_.:-]{1,64}$`. Do not accept an email address shape as a special case. These ids are opaque.
- `GET /api/v1/email-suppressions` paginated like the other list endpoints (`limit`, `offset`).
- `GET /api/v1/email-handoffs` optional filters `status`, `sku`, `store_id`, same pagination.
- Do not add a "send" route.

OpenAPI models live in `infrastructure/api/schemas.py`. Domain errors use the existing `{"error": {"code", "message"}}` shape.

### Metrics and logs

Add `catalogguard_email_handoffs_total{result}` with results `handed_off`, `suppressed`, and `expired`. Increment once per new decision, not when a duplicate insert is skipped.

Log `email_handoff_recorded` with draft id, sku, store id, status, remaining recipient count, and suppressed count. Do not log the ids.

### Tests

Add tests under `tests/`. Use the harness. Do not weaken existing assertions.

Cover at least:

- After the low-stock path, one worker run creates one `handed_off` row whose recipients are the New York customers and not `cus_1009`. The email draft is still `queued`.
- Suppress `cus_1842` before the worker runs. The handoff is `handed_off`, that id is absent from recipients and present in `suppressed_customer_ids`.
- Suppress every New York recipient. Status is `suppressed`, recipients empty.
- Run the worker twice. One handoff row, metric not doubled.
- A draft older than the max age becomes `expired` and does not include recipients. Use the harness clock, advance it or set the cap to a tiny value via settings overrides on the harness (`harness(**settings_overrides)`).
- `POST` a duplicate suppression returns 409.
- Alembic upgrade on an empty database creates `email_suppressions` and `email_handoffs`.

Keep the suite fast. No sleeps.

### Docs

Update `README.md` in the email section and the API table. Add a short curl or `Invoke-RestMethod` snippet that:

1. Assumes the existing walkthrough has already queued the New York draft
2. Suppresses nobody by default in the happy path
3. Runs `python -m catalogguard.mailer` (or `make handoff` if you add that target)
4. GETs `/api/v1/email-handoffs?sku=BKPK-28L-OLV&store_id=nyc`

State plainly that nothing was delivered.

Add the new settings to `.env.example` with the defaults above.

Add a `handoff` target to the `Makefile` if you can do it without breaking tabs. Recipes in that file use real tabs. If you are unsure, skip the Make target and document the Python module only.

### Schema migration

Add `migrations/versions/<revision>_email_handoff.py` revising `0d865ba6ff4d`. Create both tables, the check constraint, the unique constraint, the foreign key, and the status index. Implement `downgrade` by dropping the new tables.

SQLite `create_all` in tests does not run this file. The ORM models still have to match.

After the schema change, delete `catalogguard.db`, `catalogguard.db-wal`, and `catalogguard.db-shm` if they exist, then run the seed so `make run` sees the new tables and the hero SKU is back to quantity 12, version 1.

## Done when

`make test` and `make lint` are green, or the equivalent venv commands if `make` is not on PATH. `DEV_NOTES.md` exists and is accurate. You did not leave a running server. You did not commit.
