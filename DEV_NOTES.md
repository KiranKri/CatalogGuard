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

---

# DEV_NOTES: lease renewal, drain worker, and listing history

The sections above are kept as written. The redrive work is committed as `4c814aa`, and its prompt as `f473764`. This section's work is committed separately on top of those.

## What I built

### Feature A: lease renewal, drain worker, and backlog

- **Renewal in the repository.** `OutboxRepository.renew(token=, now=, message_ids=)` returns `renewed`, `idle`, or `lost`. The constants live in `application/ports.py`.
  1. It first checks whether any still-unpublished row among the batch ids has a null or different token. If so, the result is `lost`, and nothing is updated.
  2. Otherwise it sets `claimed_at = now` on unpublished rows holding this token (`WHERE published_at IS NULL AND claim_token = token`) and returns `renewed`, or `idle` if no row matched.
  Published rows are never touched. Zero rows updated on its own is `idle`, not `lost`.
- **Keeper in the bus.** `InProcessEventBus` starts one keeper task per claimed batch, before the first handler, and cancels it in a `finally` once the batch is done. Between renewals it waits `OUTBOX_LEASE_RENEW_SECONDS` through `self._sleep`, which is `asyncio.sleep` in production and replaceable in tests.
  - `idle`: the keeper stops quietly.
  - `lost`: it increments `catalogguard_outbox_lease_lost_total` once, logs `outbox_lease_lost` with the token only, flags the batch, and stops.
  - A renew that raises is logged and retried on the next tick.
- **Drain loop on a lost batch.** The loop dispatches nothing more from a lost batch and skips `mark_published` for a handler that returned after the loss. A lost lease is not counted as a drain failure and writes no dead letter. `outbox_decode` quarantine is unchanged apart from stopping once the lease is lost.
- **Startup check.** `0 < OUTBOX_LEASE_RENEW_SECONDS < OUTBOX_CLAIM_TIMEOUT_SECONDS` is enforced in `build_runtime`, so `create_app` and the drain worker both raise `RuntimeError` with the setting name. The defaults, 60 and 300, pass.
- **Drain worker.** `python -m catalogguard.drain` and `make drain` (real tab, `$(PYTHON)`) build the API runtime, drain until idle, print `{"unpublished_before": N, "unpublished_after": M}`, and exit. It creates tables only on SQLite.
- **Backlog.** `GET /api/v1/outbox/summary` returns `unpublished`, `claimed`, and `oldest_unpublished_age_seconds`. It uses the injected clock, never negative, truncated toward zero, and null when nothing is pending. The service is `OutboxSummaryService` in `application/queries.py`. The gauges `catalogguard_outbox_unpublished` and `catalogguard_outbox_oldest_unpublished_seconds` are set when the summary is read and at the end of every drain. A drain only logs a failure of that read and never fails because of it. No payloads are read or logged.

### Feature B: listing history and rollback

- **Table.** `store_listing_revisions`, in the ORM and in revision `5751f906cab6` (parent `c81f0f63dcdc`):
  - columns: `id`, `sku`, `store_id`, `version`, `published_draft_id`, the five text fields, `published_at`
  - unique `uq_store_listing_revisions_version (sku, store_id, version)`
  - index `ix_store_listing_revisions_published (sku, store_id, published_at)`
  - FK `fk_store_listing_revisions_published_draft_id` to `listing_drafts.id`
  The downgrade drops the index and the table. Earlier revisions are untouched.
- **Writes.** `ListingService.publish` appends one revision in the same transaction as the live row, only on the branch that stores a new version. The revision copies the stripped `saved` text, never the draft text. The "same draft, matching `expected_version`" branch returns before that and writes nothing.
- **Rollback.** `ListingService.rollback` checks, in this order: no live row (404), no such revision (404), revision equal to the live version (409 `That revision is already live`). Otherwise it calls the existing `publish` with that revision's draft id and the request's `expected_version`. There is no second compare-and-set.
- **HTTP.**
  - `GET /api/v1/listings/{sku}/stores/{store_id}/revisions`: paginated, newest version first.
  - `POST /api/v1/listings/{sku}/stores/{store_id}/rollback`: body `{revision_version, expected_version}`; returns the live listing.
  - There is no unpublish and no revision delete.
- **No backfill.** A listing published before this revision has no history until its next publish. The README says so.

### Docs and setup

- **README.** The scaling paragraph is rewritten: renewal, idle versus lost, the drain worker, the summary endpoint, Compose stays inline, and "two writers" now names the drain worker. Also updated: the inline-dispatch paragraph, the publish paragraph (revisions, rollback, no backfill), the mailer table-creation rule, the API table, the configuration defaults, the migrations note, and the tests note. There is a short PowerShell snippet for `make drain` and the summary GET. The inventory curl walkthrough is unchanged.
- **`.env.example`.** Adds `OUTBOX_LEASE_RENEW_SECONDS=60`. No drain batch setting, because the worker drains until idle and the code does not need one.
- **Local database.** Recreated with `make seed`. This is recorded in the final check below.

## Where I adapted the prompt to the repository

- **Section 1, the redrive commit.** The prompt said `main` was `8deb824` with the redrive uncommitted. In the repository, the redrive code was already committed on `main` as `4c814aa`. Only `claude_dev_prompt_redrive.md`, one of the listed paths, was still untracked, so `git status` was not clean and the skip rule did not quite apply. I committed only that file, as `f473764` on `main`. I did not amend `4c814aa`. I gave it an accurate message instead of "Add dead-letter redrive.", because the commit contains only the prompt. `claude_dev_prompt_reliability.md` was not staged.
- **Partial steal.** The prompt's `renewed` and `lost` definitions overlap when another worker takes part of a batch. `lost` wins, so this worker neither renews nor keeps dispatching rows someone else now owns. Rows it still held are left until their lease expires.
- **Drain worker always runs inline.** It copies the settings with `EVENT_DISPATCH=inline`, because an async `drain()` would return before any work happened. The API default and Compose are unchanged.
- **Gauges in the bus.** The gauges are set at the end of every drain, in any process, because the bus cannot tell whether it runs in the API or the worker. That is harmless in the worker, whose gauges die with it, as the README says.
- **`lost` metric location.** The metric is counted by the bus keeper, not the repository. The repository-level "stolen" test therefore asserts `lost` and that the old token cannot publish. The metric increment is asserted in the bus-level lost-batch test.
- **Migration test chain.** `test_migrations.py` now expects head `5751f906cab6`, and checks both links of the chain: `5751f906cab6` → `c81f0f63dcdc` → `15e685dc71f6`.

## How I tested

- **Suite and lint.** `make test`: 82 passed (64 before, plus 13 in `tests/integration/test_outbox_lease.py` and 5 in `tests/integration/test_listing_revisions.py`). `make lint`: clean.
- **Feature A tests:**
  - **Repository renewal.** `renewed` moves `claimed_at` forward, and a claim at +301 s then takes nothing. After `mark_published` the result is `idle`, and the lost counter does not move.
  - **Repository steal.** A claim at +301 s under another token makes the old token's `renew` return `lost`. The old token's `mark_published` leaves `published_at` null, and the thief's lease is untouched.
  - **Keeper during a handler.** A handler blocked on an event, with `_sleep` replaced to advance `FakeClock` by 60 s once and then park. `claimed_at` moves to +60 s, and a claim at +310 s gets nothing. After release, the row is published.
  - **Lost batch.** `_sleep` steals both rows of a two-row batch. The keeper reports `lost` (+1 on the counter, through a metrics spy). Only one handler call happens, neither row is published by the lost worker (both still carry the thief's token), drain failures are unchanged, and there are no dead letters.
  - **Keeper after the last publish.** It returns `idle`, the lease is not flagged, and the counter does not move.
  - **Async API plus drain worker.** `event_dispatch=async`, with the API bus `drain` replaced by a no-op. After the PUTs: no draft, summary `unpublished` 2. `run_once` with the harness clock gives `{2 → 0}`, one `ready` draft, and one `queued` email. A second `run_once` gives `unpublished_after` 0.
  - **Inline harness.** The draft is there when the PUT returns, and the summary shows `unpublished` 0.
  - **CLI on a fresh database.** `main()` on a fresh SQLite file prints the zero summary and creates the tables.
  - **Summary endpoint.** Zeros and null age to start. One row, then +10 s: 1/0/10, with gauges 1 and 10. Claimed: 1. Then +301.9 s: 1/0, age 311 (truncated).
  - **Startup check.** `harness(outbox_lease_renew_seconds=0|300|301)` raises `RuntimeError`, and so does `run_once` with a bad setting. The existing `tests/unit/test_outbox_claim.py`, an unrenewed lease reclaimable at +301 s, is untouched and passes.
- **Feature B tests:**
  - **First publish.** It writes one revision, version 1, with no `[[` and equal to the live row, while the draft keeps its markers and product SEO is unchanged. Republishing the same draft at `expected_version` 1 returns version 1 and adds no revision.
  - **Second publish.** Revisions are `[2, 1]`, newest first, and pagination works.
  - **Rollback.** Rolling back to 1 at `expected_version` 2 gives live version 3 with revision 1's text and three revisions, with product SEO unchanged.
  - **Rollback refusals.** Rolling back to the live version is 409 `That revision is already live`. A stale `expected_version` is 409 `optimistic_conflict` with `current_version` 3.
  - **Product changed.** After PATCHing `color`, rolling back to 1 is 409 `conflict`, the live version stays 2, and the revision count stays 2.
  - **404s.** Rollback with no live row is 404, and so is an unknown revision.
  - **Distinct copy.** The tests use two different valid `FixedLLM` payloads, so the rollback text check cannot pass by accident.
- **Migration check, run but not added to the suite.** Alembic `compare_metadata` against an upgraded SQLite file reported no differences. Downgrading to `c81f0f63dcdc` removed the table, and upgrading again restored it.
- **Live smoke test.** On a scratch database:
  1. `make drain` on a fresh seed printed the zero summary.
  2. With uvicorn running, the crossing PUT gave a summary of zeros.
  3. Publish gave v1, and a second draft gave v2. The revisions were `[2, 1]` with no markers.
  4. Rollback to 1 gave v3 using draft 1's id. Rolling back to 3 returned 409 `That revision is already live`.
  5. Both gauges read 0. The server was stopped, with no tracebacks.
- **Test-suite timing.** Some full runs took about 125 s, against about 45 s earlier. An interleaved comparison of `tests/unit` between a temporary worktree of `HEAD` and this tree (54 s and 47 s against 47 s and 49 s) shows the same speed. The variance is the machine, not this change. I removed the worktree afterwards.

## What I deliberately did not do

- **No releasing rows on lost.** On `lost`, rows still under this worker's token wait for their lease to expire. I did not clear them for immediate reclaim.
- **No renew-failure metric.** A failed renew is logged and retried on the next tick. It has no metric and does not stop the batch.
- **No daemon or batch cap.** The drain worker drains until idle, which is what the prompt asks for, so there is no batch cap or setting. There is no daemon or schedule.
- **No backfill, unpublish, or revision delete.**
- **Rollback to a revision whose draft is already live.** For example, v3 republished draft 1 and you roll back to v1. `publish`'s "same draft" branch returns the live row unchanged, so the rollback succeeds and changes nothing. I kept that `publish` behavior instead of adding a separate rule.
- **No Postgres run.** Docker is not available here, so `SKIP LOCKED` together with renewal ran only on SQLite.
- **Out of scope.** No changes to thresholds, cooldown, validation, citation rules, LLM providers, handoff rules, prompt text, Compose dispatch, or the redrive behavior.
