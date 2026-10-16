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
