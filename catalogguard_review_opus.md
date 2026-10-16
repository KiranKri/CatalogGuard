# CatalogGuard review

## 1. Context

**What I read.** All of `src/catalogguard/` (domain, application, infrastructure, `seed.py`, `migrate.py`), every test under `tests/`, both prompt files, `evals/listing_guardrail.json`, `adr/0001-kafka-event-bus.md`, the Alembic environment and the single migration, `README.md`, `pyproject.toml`, `Makefile`, `Dockerfile`, `docker-compose.yml`, `.env.example`, `.dockerignore`, and `alembic.ini`.

**What I ran.**

- `make test`: 38 passed, about 9 s, all on SQLite. Make was installed (GNU Make 4.4.1) for this run. The Makefile correctly picked up `.venv/Scripts/python.exe` on Windows.
- `make lint`: `ruff check` passed and `ruff format --check` reported 71 files already formatted.
- A throwaway probe script, kept outside the repository, that builds the real app with `create_app` against fresh SQLite files, seeds it, and drives it over HTTP through `httpx.ASGITransport`. Each probe is cited below where it backs a finding. In short:
  - **A.** Cross 12 → 0, then publish the draft that the crossing stored.
  - **B.** Read the email draft from that same sell-out.
  - **C.** Generate a draft, PATCH the product's attributes, then publish the old draft.
  - **D.** Put one undecodable row in `event_outbox`, then PUT a real crossing.
  - **E.** With a fake LLM that sleeps 1.5 s, send a crossing PUT and an unrelated PUT at the same time.
  - **F.** Send the same Idempotency-Key twice at the same time.
  - **G.** Call `validate_listing` on copy that pairs a wrong value with a valid citation.

**What I did not run.** Docker Compose, Postgres, and a real LLM provider. Every statement below about Postgres behavior comes from reading the code and from how Postgres is documented to behave. I label those as not reproduced.

**Where the repository disagrees with the review prompt or with itself.** The review prompt matches the code. A few documentation claims are stronger than the code supports. Each is noted inside the finding it belongs to:

- the bus docstring says a permanent failure "cannot wedge the drain" (finding 2)
- the README says handlers are "safe to run again" (finding 6)
- the ORM docstring says the schema comes from `create_all`, but this also runs on Postgres in production (finding 11)
- `test_concurrency.py` contains no concurrency (appendix)
- the OpenAPI description says a crossing writes `InventoryChanged`, when every change does (appendix)

## 2. What is in good shape

Future changes should keep these, so I list why each one works.

**The domain rules are small, pure, and pinned at their edges.**

- `domain/threshold.py` and `domain/crossing.py` import nothing but the standard library.
- The tests cover the cases that usually go wrong:
  - an explicit `0` override wins over a broader value
  - a first insert is not a crossing
  - 4 → 2 is not a new crossing
  - the cooldown is exclusive at exactly 24 h
- The README, the module docstrings, and the code agree on these rules. In my experience that is rare, and worth protecting.

**The inventory write is a correct compare-and-set inside one transaction.**

- `InventoryService._apply` writes three things in one unit of work: the row (`UPDATE … WHERE version = :expected`), the idempotency record, and the `InventoryChanged` outbox row.
- The request hash includes the path parameters. It also distinguishes "`threshold_override` omitted" from "`threshold_override: null`" through an `__unchanged__` sentinel. That three-way handling is easy to get wrong, and here it is right.
- A no-op write does not bump the version and does not emit an event.

**No transaction is held open across the LLM call.**

- `ListingService.generate` reads in one unit of work, reserves budget in a second short one, and calls the model outside any transaction.
- It then writes in a third unit of work and checks `trigger_event_id` again before inserting.
- This is the right shape. Nobody should "simplify" it into one transaction. That would hold a database connection, and on SQLite the write lock, for the whole model latency.

**The cost reservation is atomic.**

- `LlmUsageRepository.try_consume` is a single conditional upsert (`ON CONFLICT … DO UPDATE … WHERE call_count < cap`), committed before the provider is called.
- Two concurrent callers cannot both take the last unit. On SQLite this behaves correctly in the tests. On asyncpg I expect the same `rowcount` behavior but did not run it.

**Idempotency is enforced by the database, not by hoping the handler is careful.**

- `listing_drafts.trigger_event_id` and `email_drafts.dedupe_key` are unique.
- When a handler loses a race on one of these, it raises `ConflictError`. The bus retries, and the retry returns the row that already exists.

**Failures are classified carefully.**

- Validation failures are deterministic, so they are stored on the draft rather than retried or dead-lettered.
- Malformed JSON is retried.
- HTTP 400-class responses from the provider are permanent. Timeouts, 429, and 5xx are transient.
- The strict `json_schema` sent to the provider is generated from the same `LISTING_LIMITS` the validator uses, so the limits have one source.

**Drafts are kept separate from live copy, and the tests check it.**

- Every listing test asserts that `Product.seo_*` is unchanged.
- Publishing is optimistic-versioned.
- The email body is a template, so customer ids never reach the model.

**The Kafka skeleton fails loudly.** `KafkaEventBus` raises on every method. A half-wired broker cannot look healthy.

**The README is honest about its tradeoffs.** It documents the dropped-crossing cooldown, the gap for uncited claims, and the fact that budget is spent even when a call fails. My findings target the places where the code falls short of that honesty, not the tradeoffs it already names.

## 3. Findings

Ordered by how much I think they matter for a production catalog service.

### 3.1 Published store listings contain raw `[[key]]` citation markers

**What I observed.**

- The guardrail requires every cited attribute to appear in the copy as `[[key]]` (`application/listing_validation.py:68-83`).
- The prompt tells the model to do this (`prompts/listing_rewrite/v1.md:6`), and the stub does it too (`infrastructure/llm/stub.py:60`).
- `ListingService.publish` copies the draft's text fields onto `store_listings` verbatim (`application/listing.py:142-153`).
- Nothing strips or renders the markers. Probe A published the seeded backpack's draft, and `GET /api/v1/listings/BKPK-28L-OLV/stores/nyc` returned:

  > Fieldwork Supply Trailhead 28 Pack at Hudson Market. 28 [[capacity_l]] olive [[color]] ripstop nylon [[material]] 890 [[weight_g]]

- The integration test asserts that `[[color]]` is present in the draft. No test looks at what publish produces.

**Why it matters.**

- `store_listings` is described as the live, store-scoped copy. Any consumer that renders it as-is shows template syntax to shoppers. If markers ever appear in `meta_description`, they show up in search snippets.
- The length limits (title ≤ 70, meta ≤ 160) are measured on the marked-up text. If someone strips markers later, the rendered lengths no longer match what was validated.
- For booleans the format check requires the literal token `true`, so the copy reads like "true [[dishwasher_safe]]". That is a sign the marker format was designed for validation, not for reading.

**Direction.** This needs a decision about where citations live. Each option below is responsible:

- Keep markers in the draft. Render to clean text at publish time, and validate lengths on the rendered text.
- Move citations out of the prose into structured data, for example `{"field", "start", "end", "key"}`, so the copy is clean from the start.
- Declare that `store_listings` holds marked-up source, that the storefront must render it, and document that contract.

Whichever is chosen, add a test that publishes a stub draft and asserts the live row has no `[[`. If the third option is the intent, then this is an undocumented contract rather than a bug. It still needs the documentation and the test.

**Severity: high.** It affects every published listing on the one path that ships copy to customers.

### 3.2 An outbox row that cannot be decoded wedges its batch, and drain errors turn committed writes into 500s

**What I observed.**

- `InventoryService.upsert` commits and then awaits `bus.drain()` (`application/inventory.py:54`). An exception raised in the drain goes straight to the route.
- `_drain` claims up to 50 rows, then calls `load_event` on each row before dispatching it (`infrastructure/events/bus.py:78-92`). `load_event` indexes every dataclass field (`events/serialize.py:34`).
- `_run_one` catches handler exceptions. It does not catch decoding errors, or errors in `mark_published` or `_dead_letter`.

Probe D inserted one `InventoryChanged` row that was missing `correlation_id`, then sent a real crossing PUT:

- The PUT returned **500**, but the write had committed: quantity 2, version 2.
- The crossing produced **no drafts**. Its outbox row was claimed in the same batch as the bad row, and that lease was never released.
- A retry with the same key returned 200 with `Idempotency-Replayed: true`. Clients that retry correctly do recover.

**Why it matters.**

- The batch is wedged for good, not just for one lease window. Rows are claimed in `id` order. Once the 300 s lease expires, the next drain claims the bad row again, along with every other unpublished row, and dies on it again. Each lease cycle therefore pulls more real events into the stuck set, and each cycle gives one inventory PUT a 500.
- In `async` mode the same exception kills the background task, and nothing is logged beyond asyncio's "Task exception was never retrieved".
- The realistic trigger is not a hand-edited row. It is adding a required field to an event dataclass and deploying while unpublished rows exist, which is a normal rolling deploy.
- The bus docstring says "a permanent failure cannot wedge the drain" (`bus.py:9-11`). That holds for handler failures, not for decoding.
- No metric would show any of this.

**Direction.** These are independent, and any subset helps:

- Treat a decode failure as a per-row poison message: write a dead letter, then mark the row published or quarantined.
- In the request path, catch drain errors, then log and count them. The response the client gets is about the committed write, not about downstream work.
- Version event payloads, for example with a `schema_version` field and default values for added fields. This matters more once the payload goes onto Kafka as a contract.
- Add a gauge for the oldest unpublished outbox row. That one number would also expose findings 3.6 and 3.7.

**Severity: high.** Inventory is the source of truth, and this makes its write API return errors for writes that succeeded while low-stock events silently stall.

### 3.3 Publishing trusts a draft that was validated against an older catalog

**What I observed.**

- `publish` refuses only drafts with status `validation_failed` (`application/listing.py:136`). It does not check the draft against the current product.
- In probe C:
  1. I generated a draft for the backpack (28 L, olive, ripstop nylon).
  2. I PATCHed the product to 30 L, black, recycled polyester (200).
  3. I published the old draft (200).
- The live listing now says "28 … olive … ripstop nylon".

**Why it matters.**

- The guardrail's purpose is to keep copy tied to catalog facts. It runs once, at generation time. Drafts can sit for days before an operator publishes them, and attribute corrections are exactly the edits that make old copy wrong.
- A related detail: `status = "published"` is set on each draft that goes live and never cleared. So the status means "was live at some point", not "is live now".
- Republishing an older draft rolls the live listing back. That may be intended, but it is not documented.

**Direction.**

- Re-run `validate_listing` against the current attributes at publish time. It is cheap and deterministic, so it could simply be part of publish.
- Alternatively, store a hash of the attributes, or the product's `updated_at`, on the draft and refuse or flag a mismatch.
- Either way, add a "PATCH, then publish" test.

**Severity: medium.** It needs a product edit between generation and publish. When that happens, the result is wrong product facts in live copy, and a probe confirmed it.

### 3.4 The low-stock email can make a false scarcity claim, including "Only 0 left — act now"

**What I observed.**

- The template is `Only {{quantity}} left in {{store_name}} — act now` (`prompts/email_urgency/v1.md:1-7`).
- A crossing to 0 is valid: the previous quantity was at or above the threshold and 0 is below it. Probe B crossed 12 → 0 and got the subject "Only 0 left in Hudson Market — act now".
- The quantity is fixed at event time. The draft carries no as-of time, no expiry, and no instruction that the mailer must check stock again before sending.
- The listing prompt forbids "fake urgency". The email template is urgency by design.

**Why it matters.**

- A sold-out item generates an "act now" email to every recent viewer, for something they cannot buy.
- Once a downstream mailer adds delay, which is the whole point of queuing, the number may be wrong in either direction. A restock makes "only 2 left" false.
- Consumer-protection rules about misleading urgency and scarcity are aimed at this kind of claim. I am not making a legal judgment. It is worth a check with whoever owns marketing compliance.

**Direction.**

- Skip the draft at quantity 0, or use a different template such as "back-in-stock alerts".
- Add `quantity_as_of` and `valid_until` to the payload and document that the mailer must check stock again.
- Or let the mailer render the quantity at send time.

The README already makes the mailer responsible for suppression lists. A per-customer frequency cap belongs in the same conversation: a customer who viewed thirty SKUs that all cross on the same day gets thirty drafts.

**Severity: medium.** It is customer-facing, it happens on every sell-out crossing, and the fix is small.

### 3.5 The guardrail's value check is not tied to the citation, and the eval set is too small to show it

**What I observed.**

- The prompt says "the value next to each cited attribute must be the exact value". The validator checks a weaker rule: the value must appear as a token anywhere in the five fields joined together (`listing_validation.py:35-39, 79`).
- Probe G used product attributes `capacity_l=28, color=olive` and copy containing "A 35 liter [[capacity_l]] pack in black [[color]]". It returned **no violations**, because "28" appears in the title "Trailhead 28 Pack" and "Olive" appears elsewhere.
- `evals/listing_guardrail.json` has four handwritten cases, and none of them pairs a wrong value with a right citation.

**Why it matters.**

- The README is careful to call this "a citation check, not a proof", and that is fair. But the specific property it claims ("the attribute's value appears as its own token") is satisfied by text that cites an attribute and states the wrong value right next to the citation. That is the failure a citation check exists to catch.
- Numeric attributes make it likely: sizes, capacities, and weights often also appear in product names.

**Direction.**

- Anchor the check: for each `[[key]]` occurrence, require the formatted value within a small window just before it.
- Grow the eval set with adversarial cases: a wrong value next to a citation, a value only in another field, a bare number claim with no citation.
- Separately, an eval that runs the real model on the seed catalog would measure something the handwritten cases cannot (see section 5).

**Severity: medium.** It weakens the service's main safety claim, and closing the gap is cheap.

### 3.6 Draining from several instances can duplicate low-stock events and emails on Postgres (not reproduced)

The README says that "several API processes can therefore drain one Postgres outbox". Two mechanisms make that weaker than it sounds.

**The lease is not fenced, and it is not sized to the work.**

- `claim` leases up to 50 rows under one timestamp (`repositories.py:361-392`).
- `_drain` then processes them one at a time, never renews the lease, and never checks `claim_token`. `mark_published` does not filter on the token either (`repositories.py:394-400`).
- With a real model during an outage, one `LowInventoryDetected` can take about 3 handler attempts × (3 × 30 s timeout + backoff), roughly 4.5 minutes. That is close to the 300 s lease on its own.
- A batch with several such events outlives its lease, and another instance reclaims and processes the tail while the first instance is still working through it.

**The processed-event guard swallows its own conflict.**

- `ProcessedEventRepository.mark` catches `IntegrityError` and returns (`repositories.py:411-423`).
- Take two concurrent `ThresholdHandler` runs for the same `InventoryChanged` on Postgres:
  1. Both pass `seen`.
  2. Both publish a `LowInventoryDetected` with a fresh `uuid4` (`handlers.py:70`).
  3. Both update the cooldown row. If the row already exists, both UPDATEs succeed under READ COMMITTED. The second one waits for the first to commit, then applies its own change.
  4. The second `mark` hits the primary key, the error is swallowed, and the second transaction commits a second event.
- The email `dedupe_key` includes the new event id (`email_drafts.py:63`), so the second email draft is not deduplicated. Customers would get two emails, and the second listing draft costs another model call.
- The README's statement that "threshold evaluation records `processed_events` in the same transaction as the downstream outbox row" is true. It does not give the protection it implies, because the conflict is swallowed.
- On SQLite this race is masked: writes are serialized, and a transaction working from a stale snapshot fails when it tries to write.

**What would change my mind.** A Postgres test with two drainers and a handler forced to outlive the lease, showing exactly one `LowInventoryDetected`.

**Direction.** Any of these closes most of the gap:

- Derive the `LowInventoryDetected` event id deterministically from `causation_id`, for example with `uuid5`. The outbox's existing unique `event_id` constraint then rejects the duplicate, and the drafts deduplicate as a result.
- Make `mark` the first write and abort if it conflicts, instead of continuing.
- Lock the cooldown row (`SELECT … FOR UPDATE`) or make the cooldown upsert conditional.
- Claim smaller batches or renew the lease per row, and check `claim_token` when marking a row published.

**Severity: medium.** It needs multi-instance Postgres plus slow handlers, but that is the deployment the README describes.

### 3.7 Inline dispatch ties every inventory write in a process to model latency, and recovery depends on traffic

**What I observed.**

- `_drain` holds a process-wide `asyncio.Lock` and loops until the outbox is empty (`bus.py:73-92`). A PUT therefore waits for its own events, for other requests' events, and for any backlog.
- In probe E, a non-crossing PUT for an unrelated SKU took **1.63 s**, because it queued behind a crossing whose fake LLM slept 1.5 s.
- No periodic drain exists. A row whose lease expired after a crash is retried only when some later write triggers a drain, or when the process restarts.
- On startup, the lifespan drains before it yields (`api/app.py:38-41`). After a model outage with a backlog, that can delay readiness past the Docker health check's budget: a 20 s start period plus 12 × 5 s. I reasoned this through but did not run it.
- In `async` mode there is a lost-wakeup window, which I did not reproduce. If `drain()` runs while the previous task has seen an empty claim but has not yet finished, `drain()` sees a task that is not done and schedules nothing. The new row then waits for the next write.

**Why it matters.**

- The README admits that inline mode "keeps the request open for the LLM call". It does not say that inline mode also makes unrelated stock updates wait. Store systems usually send inventory updates in bursts.
- Combined with finding 3.8, slow responses also make clients retry while the first request is still running.

**Direction.**

- Keep inline mode as a demo convenience, but do not ship it as the Compose default.
- Move draining to a loop the process owns, started in the lifespan: poll on an interval and also wake on a notify signal. Requests then only signal it.
- If inline mode stays, have a request drain only its own event ids, or skip draining when the lock is already held.

**Severity: medium.** This is a latency and availability problem on the core write path. It is partly documented.

### 3.8 Postgres is the production database, but nothing runs against it, and at least one Postgres-only failure follows

**What I observed.**

- Every test uses SQLite.
- `test_migrations.py` runs Alembic on SQLite and checks four table names and three columns. It does not compare the migration with the ORM metadata.
- One concrete consequence: `X-Correlation-ID` is taken from the request with no length check (`api/app.py:66`), but `listing_drafts.correlation_id` and `email_drafts.correlation_id` are `VARCHAR(64)` (`db/orm.py:99, 146`).
  - SQLite ignores the length. Postgres rejects longer values with a string-truncation error.
  - `_savepoint_add` converts only `IntegrityError`, so on Postgres a request carrying a correlation id over 64 characters goes through these steps:
    1. The inventory write succeeds, because the outbox payload is JSON.
    2. The listing handler calls the model, fails on insert, and is retried. That is three model calls and three cap units.
    3. The listing handler is then dead-lettered.
    4. The email handler is dead-lettered too.
  - The manual rewrite endpoint would return 500 after spending a model call.
  - I did not run this on Postgres. The reasoning rests on standard `varchar(n)` enforcement.
- Other behaviors that only Postgres exercises: the `SKIP LOCKED` branch, `rowcount` semantics on asyncpg for `try_consume` and the compare-and-set, and the READ COMMITTED races in 3.6.

**Direction.**

- Run the existing suite against Postgres in CI, for example with a service container. Most tests should pass unchanged, and the ones that fail are the point.
- Bound and sanitize the correlation id in the middleware, or widen the columns.

**Severity: medium.** The suite cannot see this class of failure, and the README's production story depends on it.

### 3.9 Concurrent retries with the same Idempotency-Key get 409 instead of a replay

**What I observed.**

- `upsert` reads the idempotency record, and only then applies the write (`inventory.py:41-53`).
- Two in-flight requests with the same key both miss the record. One wins the compare-and-set and the other gets `optimistic_conflict`.
- Probe F, on SQLite, returned one 200 and one **409 `optimistic_conflict`** with `current_version: 2`, a version produced by the caller's own write.

**Why it matters.**

- Sending the same key while the first request is still running is the most common client retry, and inline LLM latency (finding 3.7) makes it more likely.
- The documented contract ("same key and body return the original response") does not hold under concurrency.
- The harm is limited because the PUT sets an absolute quantity. Still, an error like this shows up on a store associate's screen.

**Direction.**

- On an optimistic conflict, or on a conflict when inserting the idempotency record, read the key again and replay if it now exists.
- Or insert the idempotency record first in the transaction, so a duplicate request waits on the primary key and then replays.

**Severity: low.**

### 3.10 Dead letters have no redrive, and some losses cannot be recovered through the API

**What I observed.**

- `dead_letters` can only be listed.
- `catalogguard_dead_letters_total` has no `handler` label.
- The cooldown is recorded when the threshold handler fires, so it is set even if both downstream handlers later fail.
- The README offers the manual rewrite endpoint as the recovery path. That rebuilds a listing, but a dead-lettered `email_draft` cannot be recovered at all: the cooldown suppresses the next crossing, and the manual endpoint never queues email.
- `processed_events` rows written by the listing and email handlers are never read. That suggests a guard that does not exist.

**Direction.**

- Add a redrive command or endpoint that dispatches the stored payload to the named handler. The handlers are idempotent, so this is safe.
- Label the dead-letter counter by handler and alert on it.

**Severity: low.** It matters more as volume grows.

### 3.11 The schema has two sources of truth at runtime

**What I observed.**

- The lifespan always runs `metadata.create_all` (`api/app.py:38`), including on Postgres after the Docker CMD has already run `alembic upgrade head` (`Dockerfile:25`).
- `create_all` creates missing tables and never alters existing ones. An ORM change made without a migration therefore half works in Docker: new tables appear, new columns do not.
- A later migration that creates the same table then fails.
- Migrations also run in every API container's start command, so two replicas would race.

**Direction.**

- Run `create_all` only for SQLite or development.
- Add a test that runs Alembic's `compare_metadata` and asserts an empty diff.
- Run migrations as a one-shot job.

**Severity: low.** It is a maintenance trap rather than a current bug, since the one migration matches the ORM today.

## 4. Open questions

Each of these has more than one responsible answer.

**What is the low-stock rewrite for?**

- `listing_prompt_values` passes the product and store only (`application/prompts.py:37-59`). The `reason` and the quantity never reach the model.
- So a rewrite triggered by low stock produces the same kind of copy as a manual one. The trigger costs a model call per (SKU, store) crossing, but the copy does not reflect why it was produced.
- One answer: tell the prompt about the situation, for example to de-emphasize the item or suggest alternatives.
- Another: decouple rewriting from stock and run it on catalog edits.
- A third: keep it, if the real goal is to refresh copy regularly on items that are moving.
- I don't know the product intent. The current wiring suggests the trigger and the content were designed separately.

**Should copy be generated per store or per SKU?**

- The only store-specific inputs are the store's name and region.
- Per-store generation multiplies cost by the number of stores and lets copy drift between stores. Per-SKU generation plus store templating is cheaper and consistent, but less local.
- The cost cap already leans toward per-SKU: it is per SKU per day and shared across stores. A retailer with more than 24 stores would see a nationwide sell-out of one hot SKU dead-letter rewrites for most of its stores, while a catalog of 10,000 SKUs has no overall ceiling at all.
- A global daily budget plus a per-SKU cap would bound both failure modes.

**Should the crossing be decided in the write transaction?**

- Today the threshold handler reads the current overrides when it runs, not the values at the time of the write.
- With delayed dispatch (async mode, retries, lease expiry), a threshold edit made in between changes the decision.
- A threshold edit that makes current stock count as low never fires, because crossings are defined on quantity. That follows from the chosen design, but it is not written down.
- Deciding at write time and putting the threshold and its source into the event would make the decision auditable and independent of timing. The cost is more logic on the write path.

**Should the listing and the email share one cooldown?**

- The single 24 h window controls both handlers.
- Copy probably deserves a longer quiet period. Email probably deserves per-customer frequency limits more than a per-(SKU, store) window.
- Splitting them adds configuration. Keeping them together is simpler but tunes neither one well.

**What should the production default dispatch mode be?**

- Compose ships `EVENT_DISPATCH=inline`. That keeps the demo synchronous and easy to inspect.
- Every finding in 3.7 points the other way for anything carrying real traffic. Defaulting to a separate drain loop and marking inline as demo-only is one answer. Keeping inline and documenting the cost more clearly is another.

## 5. Suggested next reviews

**Run against Postgres with two drainers.**

- This would confirm or refute 3.6 and 3.8 directly.
- It would also cover `SKIP LOCKED`, asyncpg's `rowcount` for the conditional upsert and the compare-and-set, `varchar` enforcement, and the concurrent `ThresholdHandler` path with an existing cooldown row.
- It is the highest-value next step, because the README's production story rests on behavior that no one has run.

**Evaluate the real model.**

- Run `listing_rewrite/v1` against the configured model on the seed catalog plus adversarial products: long names, numeric attributes that also appear in names, boolean attributes, and prompt-injection text in `base_description`.
- Measure the validation-failure rate, uncited factual claims (judged by people or by a model-graded rubric), and where markers land.
- The current eval file tests the validator, not the model.

**Plan for event schema evolution.**

- Before the Kafka cutover in the ADR, decide how payloads are versioned and how old versions are upgraded on read, and what happens to rows in flight during a deploy (see 3.2).
- Check whether any handler depends on ordering. The ADR keys by `{sku}|{store_id}` for order, but the in-process bus gives no ordering across instances.

**Measure latency on the write path.**

- Measure PUT p50 and p99 with a slow fake LLM in inline and async modes, during bursts, and after a restart with a backlog.
- This puts numbers on 3.7.

**Review data lifecycle and PII retention.**

- `idempotency_keys`, `processed_events`, the published outbox rows, `recent_interactions`, and `email_drafts` (which hold customer ids) all grow without limit.
- Recipient lists are unbounded per draft: every interaction in the lookback window lands in one JSON payload.
- Retention, and whether to delete or archive, deserves its own pass.

**Review abuse and authorization once auth exists.**

- The manual rewrite endpoint spends money, and today it is limited only by the per-SKU cap.
- Publish changes live copy.
- Product PATCH has no optimistic concurrency, unlike inventory and listings, so an attribute correction can be silently lost to a concurrent edit.

## Appendix: maintenance nits

- `tests/unit/test_concurrency.py` sends its requests one after another. Either rename it or add a real concurrent case like probe F.
- The OpenAPI description (`api/app.py:21-28`) says a downward crossing "writes an InventoryChanged event". Every change writes one, and a crossing writes `LowInventoryDetected`.
- `application/errors.py` is a "backward-compatible alias" in a 1.0.0 repository with no prior release. It can probably go.
- The bus stores handlers as `object` and uses `# type: ignore` to call `.handle`. A small `Handler` Protocol would let the type checker see mistakes in how handlers are wired.

## What I deliberately did not review

- Docker and Compose execution, and Postgres behavior at runtime. I reasoned about them but ran neither.
- Real LLM provider behavior beyond the mock-transport tests.
- Performance at scale.
- Authentication, email delivery, a UI, payments, and carts, which are out of scope by design.
- Dockerfile hardening beyond noting the non-root user.
- The quality of the seed data as demo content.
