# DEV_NOTES: email handoff

## What I built

A downstream email handoff step. CatalogGuard still sends nothing. A one-shot worker decides, for each queued email draft, whether a mailer may take it. The low-stock path is unchanged: it keeps creating `email_drafts` rows with status `queued`, and those rows are never modified.

- **Tables.** `email_suppressions` (`customer_id` PK, `reason` ≤ 200, `created_at`) and `email_handoffs` (`id` PK, `email_draft_id` unique FK to `email_drafts.id`, `status` checked against `handed_off`/`suppressed`/`expired`, `payload` JSON, `suppressed_customer_ids` JSON, `created_at`, index on `status`). They are defined in `infrastructure/db/orm.py`, and the new Alembic revision `migrations/versions/15e685dc71f6_email_handoff.py` sits on top of `0d865ba6ff4d`. The initial revision is untouched.
- **Decision rules.** `application/email_handoff.py`:
  - `decide_handoff` is a pure function. It checks age first (`now - created_at > max_age`). Otherwise it copies the draft payload and removes suppressed ids. It returns `suppressed` when nobody is left, including a draft that started with no recipients, and `handed_off` otherwise.
  - `quantity_as_of` and every other draft field are carried through, because the whole payload is copied.
  - `EmailHandoffService.run_batch` selects up to `EMAIL_HANDOFF_BATCH_SIZE` queued drafts with no handoff row, oldest first, using the injected clock.
  - Each draft is decided in its own transaction. The insert runs inside a savepoint (`_savepoint_add`). A unique conflict becomes `ConflictError`, and the service then skips the draft: no raise, no overwrite, no metric.
- **Worker.** `python -m catalogguard.mailer` and `make handoff` load `Settings`, build an engine, create missing tables on SQLite, run one batch, print a JSON summary as the last stdout line, and exit. Wiring goes through `container.build_handoff_service`, which the API container also uses.
- **HTTP.**
  - `POST /api/v1/email-suppressions`: 201 on create, 409 `conflict` on a duplicate, 422 when `customer_id` fails `^[A-Za-z0-9_.:-]{1,64}$`. An email address is rejected by the pattern itself, not by a special case.
  - `GET /api/v1/email-suppressions`: paginated.
  - `GET /api/v1/email-handoffs`: filters `status`, `sku`, `store_id`, plus pagination.
  - There is no send route.
- **Metrics and logs.**
  - `catalogguard_email_handoffs_total{result}` counts once per new decision.
  - `email_handoff_recorded` logs the draft id, SKU, store, status, remaining count, and suppressed count. It never logs customer ids. Adding a suppression logs nothing identifying.
- **Docs.** README has a new "Email handoff" section, a walkthrough snippet after the existing curl walkthrough, API table rows, and updated configuration, PII, migrations, and tests notes. `.env.example` documents `EMAIL_HANDOFF_BATCH_SIZE=50` and `EMAIL_HANDOFF_MAX_AGE_HOURS=72`. The Makefile has a `handoff` target, with a real tab, checked with `cat -A`.
- **Local DB.** I deleted `catalogguard.db` (there was no `-wal` or `-shm`) and ran `make seed`. The new file has both new tables, and `BKPK-28L-OLV` at `nyc` is at quantity 12, version 1, with no drafts or handoffs.

## Where I adapted the prompt to the repository

- **`init_db` in the worker.** The feature spec says the worker must call `init_db`. The kept review fix says `create_all` runs only for SQLite. The worker follows the same rule as the API lifespan: `init_db` on SQLite only. On Postgres, run `make migrate` before the first handoff. A fresh SQLite database still works, and a test covers it.
- **Extra payload fields.** Every handoff payload also carries `email_draft_id`, so the mailer copy stands on its own. Expired payloads also include `draft_created_at` and `max_age_hours`. Expired payloads never include `recipients`.
- **Two `suppressed` notes.** "every recipient is on the suppression list" and "the draft had no recipients" get different `handoff_note` text, because the first one would be false for an empty draft.
- **Filtering by SKU and store.** The `sku` and `store_id` filters on `GET /email-handoffs` join `email_drafts`. The handoff table has no SKU or store columns, as specified.
- **Malformed recipients.** Recipient entries without a string `customer_id` are dropped, because they cannot be suppression-checked or mailed. The low-stock path never produces such entries.
- **Named unique constraint.** The constraint is named explicitly (`uq_email_handoffs_email_draft_id`) so the ORM and the migration agree by name.
- **Exact-age boundary.** The age check is strictly greater-than, as specified. A draft exactly 72 h old is still handed off, and a test pins that boundary.

## How I tested

- `make test`: 54 passed, the 43 existing tests plus 11 new. `make lint`: Ruff check and format clean. Make 4.4.1 was installed through winget earlier in this session, and the targets ran through it.
- `tests/integration/test_email_handoff.py` covers:
  - the New York happy path through the real worker entry point (`run_once`), with `cus_1009` absent and the draft still `queued`, with an unchanged payload
  - one customer suppressed; all suppressed
  - expiry, and the exact-72 h boundary
  - a sold-out crossing producing no draft and no handoff
  - oldest-first batching with batch size 1
  - an Austin draft with no recipients (`suppressed`)
  - the status, SKU, store, and pagination filters
  - the suppression API (duplicate is 409; email shape, a 65-character id, an over-long reason, and a missing reason are each 422)
  - `main()` on an empty database printing a zero summary
- Metric assertions compare deltas, because the Prometheus registry is shared across the whole test process.
- **Race test.** The deterministic race test is `test_race_loser_skips_without_overwriting_the_first_decision`:
  1. It decides a draft once.
  2. It adds a suppression that would change the decision.
  3. It calls `record()` again with the same, already-selected draft.
  4. It asserts the call returns `None`, that only the original row exists, and that no counter moved.
  The `asyncio.gather` of two `run_batch()` calls in `test_second_run_does_not_insert_or_count_again` is only a smoke check. On SQLite the two runs may or may not interleave, and the assertion holds either way.
- `tests/integration/test_migrations.py` now also asserts, after upgrading an empty SQLite file twice, the columns of both new tables, the `ix_email_handoffs_status` index, a unique index on `email_draft_id`, the foreign key to `email_drafts.id`, and that `alembic_version` is at `15e685dc71f6`.
- **Manual checks, run but not added to the suite:**
  - Alembic's `compare_metadata` against an upgraded SQLite file reported no differences from the ORM. Downgrade to `0d865ba6ff4d` removed both tables, and upgrading again restored them.
  - An end-to-end run on a scratch database: seed, start uvicorn, send the walkthrough PUT, `make handoff`, GET the handoffs, `make handoff` again, then stop the server. The output matched the README: first run `handed_off: 1`, the four New York recipients, the draft still `queued`, second run `examined: 0`, and suppression POSTs returning 201, 409, 422. No server was left running.

## What I deliberately did not do

- **No sending.** No SMTP, no provider client, and no send route. No scheduler or daemon either: the worker runs one batch per invocation.
- **Postgres not run.** Docker is not available here. On Postgres, two workers can select the same pending drafts. The loser's insert waits on the winner's uncommitted unique key and then hits the conflict, so the result is wasted work, not a double decision. If contention ever matters, `FOR UPDATE SKIP LOCKED` on the pending select would avoid the wasted work.
- **Metrics from the worker process.** The counter is incremented in the worker, which exits after one batch, so the API's `/metrics` never sees those increments. I did not add a Pushgateway or a textfile collector. The JSON summary and the `email_handoffs` rows are the durable record. This is stated in the README.
- **Decisions are final.** A suppression added after a decision does not revisit it, as specified. There is no delete or update endpoint for suppressions, and no admin path to re-decide a draft.
- **No index for the pending query.** I did not add an index on `email_drafts (status, created_at)`. The pending query is an anti-join backed by the unique index on `email_handoffs.email_draft_id`. At volume, a partial index on queued drafts would be the next step.
- **Run aborts on non-conflict errors.** If one draft fails with something other than the unique conflict, for example a database outage, the run stops there and the CLI exits non-zero. Decisions already committed are kept, and the next run continues.
- **Out of scope.** No changes to thresholds, cooldown, listing validation, the LLM providers, Docker, or the earlier review fixes. Nothing was committed, and this directory is not a git repository.

---

# DEV_NOTES: dead-letter redrive

The email handoff notes above are unchanged from when that feature landed. Their test count (54) was the suite size at that time. The repository has since been committed as `8deb824`. This feature is committed separately on top of it.

## What I built

Operators can recover a handler dead letter without SQL and without replaying every subscriber on that event.

- **Endpoint.** `POST /api/v1/dead-letters/{id}/redrive` takes no body; a body is ignored. It decodes the stored event and calls `handle` once on the one subscribed handler instance whose name matches the letter. There is no `_run_one`, no retry, and no new dead letter, whatever happens. The letter is never edited or deleted, and nothing goes back onto the outbox.
- **Table.** `dead_letter_redrives`: `id`, `dead_letter_id` (FK `fk_dead_letter_redrives_dead_letter_id`), `status` (check `ck_dead_letter_redrive_status`: `succeeded` or `failed`), `error` (empty on success, otherwise truncated to 2000 characters), `created_at`, and index `ix_dead_letter_redrives_dead_letter_id`. It is in the ORM and in the new revision `migrations/versions/c81f0f63dcdc_dead_letter_redrive.py`, whose `down_revision` is `15e685dc71f6`. The downgrade drops the table. The earlier revisions are untouched.
- **Rules.** In `application/redrive.py`, `DeadLetterRedriveService` applies them in order:
  1. An unknown id is 404.
  2. `outbox_decode` is 409 `Undecodable outbox rows cannot be redriven`. `load_event` is not called and nothing is inserted.
  3. If any `succeeded` row exists, the response is 200 `replayed` with the oldest success row's id, and the handler is not called.
  4. A payload that fails to decode is 409.
  5. A handler name that is not registered is 409.
  6. Otherwise the handler runs once. On success, the service commits a `succeeded` row, then calls `drain()` once and returns 200. On any exception, it commits a `failed` row and re-raises, so the existing mappings answer: `CostLimitError` 429, `NotFoundError` 404, `MalformedLLMOutputError` 502, `ConflictError` 409, and anything else 500. A failed row never blocks the next POST.
- **Handler lookup.** `InProcessEventBus.handler_for(event_type, name)` is the new public lookup. `_run_one` and the lookup share one `handler_name()` helper, so the name stored on a letter and the name looked up cannot drift. The application service sees the bus through two small ports in `application/ports.py`, `HandlerRegistry` and `EventHandler`. It gets `load_event` injected as `decode_event`, so the application layer still does not import infrastructure.
- **List fields.** `GET /api/v1/dead-letters` keeps its pagination and adds `redrive_count`, `last_redrive_status`, and `last_redriven_at`. "Last" is the newest `created_at`, then the greatest `id`.
- **Metrics and logs.** `catalogguard_dead_letter_redrives_total{result}` with `succeeded`, `failed`, `rejected`, and `replayed`, incremented once per POST. `catalogguard_dead_letters_total` is never touched by a redrive, and its `handler` label is unchanged. The log line is `dead_letter_redriven` with the letter id, handler, event id, and result. The payload is never logged.
- **Docs.** README has a "Recovering a dead letter" paragraph in the production notes, two API table rows, a placeholder `curl.exe` example, the migrations note, and the tests note.

## Where I adapted the prompt to the repository

- **Unknown ids count as `rejected`.** The prompt says to count every POST once and defines `rejected` as the 409 refusals. An unknown id is also a refusal that runs no handler and inserts no row, so it is counted as `rejected` rather than left uncounted.
- **Drain-failure counter.** `InProcessEventBus.drain()` already increments `catalogguard_drain_failures_total` in inline mode before it re-raises. The redrive's quiet drain only logs, as `InventoryService` does after a committed write, so the counter moves exactly once. A test asserts +1. Incrementing again in the redrive would double count.
- **Same-process concurrency.** The service holds a per-letter `asyncio.Lock` in a `WeakValueDictionary`. Two concurrent POSTs in one process serialize: the first runs the handler, and the second replays. Across processes there is no lock (see below).
- **`list` kept as is.** `DeadLetterService.list` still returns plain `DeadLetter` objects, because `tests/unit/test_event_bus.py` reads `.handler` and `.attempts` from it. The HTTP list uses a new `list_with_redrives` that returns a `DeadLetterView`.
- **Head revision in the migration test.** `tests/integration/test_migrations.py` asserted `alembic_version == 15e685dc71f6`. The prompt asks for the new head, so that assertion now expects `c81f0f63dcdc`, and the test also checks through Alembic's script directory that `15e685dc71f6` is its parent. The new table's columns, index, and foreign key are asserted alongside the earlier tables.
- **Empty error messages.** An exception with an empty message is recorded with its class name, so a `failed` row always says something.

## How I tested

- **Suite and lint.** `make test`: 64 passed, which is the 54 tests from before plus 10 in `tests/integration/test_dead_letter_redrive.py`. `make lint`: Ruff check and format clean.
- **New tests:**
  - **Malformed listing output.** The same `FixedLLM` is switched to valid JSON with the new `set_content`. One `ready` draft for the letter's trigger, one `queued` email, the letter kept, and `last_redrive_status` `succeeded`. A second POST replays: a spy on `handle` sees no call, `FixedLLM.calls` does not move, and the dead-letter counter is unchanged.
  - **Concurrent POSTs.** One `succeeded`, one `replayed`, and one LLM call.
  - **Daily cap.** With a cap of 1: 429 `llm_cost_limit` on the same UTC day, recorded as `failed`, with only the manual draft present. After `FakeClock.advance(days=1)` the redrive succeeds and a draft exists for the letter's trigger id. Still one letter, and the dead-letter counter is unchanged.
  - **`outbox_decode`.** 409 with the exact message, the letter kept, `redrive_count` 0, the outbox row count unchanged, and `rejected` +1.
  - **Other refusals.** An unknown id is 404. An undecodable payload and an unregistered handler name are each 409 with their messages, and no row is written.
  - **Email recovery.** `handle` is patched to raise during the crossing and then restored. The redrive creates exactly one `queued` email, and the listing draft count stays 1. A second POST replays without calling `handle`.
  - **Threshold recovery.** A `threshold_evaluation` letter is inserted through the repository with `dump_event` of an `InventoryChanged` 10 → 2. The redrive returns with one listing draft, whose trigger is the UUID5 of the inventory event id, and one `queued` email already present. No `email_handoffs` row exists. A second POST replays and the counts do not change.
  - **Drain failure.** The bus's drain is forced to raise after a successful threshold redrive. The redrive still returns 200, the drain-failure counter moves +1, and no new dead letter is written.
  - **Failed attempt, then retry.** A generic handler error propagates (500). The `failed` row's error is truncated to exactly 2000 characters. After the clock moves, a retry succeeds: `redrive_count` 2, last status `succeeded`.
- **Migration check, run but not added to the suite.** Alembic `compare_metadata` against a SQLite file upgraded to head reported no difference from the ORM. Downgrading to `15e685dc71f6` removed the table, and upgrading again restored it.
- **Live smoke test.** On a scratch database with `LLM_DAILY_CAP_PER_SKU=1`:
  1. Seeded the database, started uvicorn, sent a manual rewrite, then the crossing PUT.
  2. The redrive (sent with a JSON body, which was ignored) returned 429 and was listed as `failed`, with `redrive_count` 1.
  3. An unknown id returned 404.
  4. `/metrics` showed `failed` 1 and `rejected` 1, with `catalogguard_dead_letters_total{handler="listing_rewrite"}` still 1.
  5. The server was stopped.
- **Local database.** I deleted `catalogguard.db` and re-ran `make seed`. Both are recorded in the final check below.

## What I deliberately did not do

- **No lock across processes.** Two API processes that receive the first POST for the same letter at the same moment can both run the handler. The handlers' own idempotency keeps it to one listing draft (unique trigger id), one email draft (unique dedupe key), and one `LowInventoryDetected` (`processed_events` plus the UUID5 id). A concurrent listing redrive can still spend two LLM calls, and two `succeeded` rows can exist. The replay then returns the oldest. A unique partial index on successful rows, or a row lock, would close that. I left it out because more than one row per letter is allowed by design.
- **No retrying or rewriting.** No retry loop, no replacement payload, and no editing, deleting, or archiving of dead letters.
- **No bulk or automatic redrive.** No bulk redrive, no CLI, and no scheduled redrive.
- **No redrive of `outbox_decode` rows.** Those need a human to fix or discard the raw row.
- **No auth.** The endpoint is as unauthenticated as the rest of the API. A listing redrive spends LLM budget through the normal per-SKU cap.
- **No Postgres run.** Docker is not available here, so the redrive path ran only on SQLite.
- **Out of scope.** No changes to thresholds, cooldown, listing validation, citation rules, the LLM providers, the handoff decision rules, the prompt text, Compose, or inline dispatch.
