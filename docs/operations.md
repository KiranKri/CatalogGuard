# Operations

## Configuration

See `.env.example`. Defaults: SQLite file `catalogguard.db`, stub LLM, inline dispatch, threshold 5, cooldown 24 hours, 24 LLM calls per SKU per UTC day, outbox lease 300 seconds renewed every 60 seconds while a drain is alive, redrive claim timeout 300 seconds renewed every 60 seconds while the handler runs, three handler attempts, three LLM transport attempts, email handoff batch 50, and handoff max age 72 hours.

`LLM_PROVIDER=openai` without `LLM_API_KEY` or `XAI_API_KEY` fails at startup.

## Postgres with Compose

```bash
docker compose up --build
```

Compose starts Postgres and the API, with `SEED_ON_STARTUP=true` and the stub provider. The Compose smoke job in GitHub Actions run [39130397208](https://github.com/KiranKri/CatalogGuard/actions/runs/39130397208) brought that stack up, ran the crossing walkthrough against `http://127.0.0.1:8000`, and ran the mailer. It did not run `python -m catalogguard.drain`. `docker compose down -v` drops the database volume.

## Scaling

SQLite is the dev default. Do not run two writers against one SQLite file. That means two API processes, or the API plus `make drain`. Postgres is the database when more than one process drains the outbox. On SQLite, SQLAlchemy issues `BEGIN IMMEDIATE` itself, so savepoints take part in the unit-of-work transaction and a failure rolls back the processed-event mark with the rest of the work.

- **Leases.** The drain leases each batch before dispatch. On Postgres that lease is `SELECT … FOR UPDATE SKIP LOCKED`, and `mark_published` only succeeds for the token that holds the lease. A lease older than `OUTBOX_CLAIM_TIMEOUT_SECONDS` (300) can be taken again. The low-stock event id is derived from the inventory event id, so two drainers cannot insert two downstream events for one write.
- **Renewal.** While a batch runs, a keeper task renews its lease every `OUTBOX_LEASE_RENEW_SECONDS` (60), so a slow model call no longer lets another worker take rows that are still being worked on. The renew interval must be greater than 0 and less than the claim timeout, or the API and the drain worker refuse to start.
- **Idle versus lost.** A renewal that finds nothing left under its token is **idle**, the normal end of a batch, and the keeper stops quietly. A renewal that finds an unpublished row from its batch under another token, or under none, is **lost**. The keeper then counts `catalogguard_outbox_lease_lost_total` once and logs `outbox_lease_lost` with the token. This worker dispatches nothing more from that batch and does not mark the in-flight row published. A lost lease is not a drain failure and writes no dead letter. Handlers stay idempotent, so the new owner can run them again. A lease left to expire without renewal is still reclaimable after the timeout.
- **In one process.** The asyncio lock only stops a single process from draining itself twice. Listing and email workers that should run outside the API still belong on Kafka.

## Draining without the API

`python -m catalogguard.drain` (or `make drain`) builds the same handlers and bus as the API, drains inline until no claimable row is left, prints `{"unpublished_before": N, "unpublished_after": M}` as its last stdout line, and exits. That command is tested on SQLite only. The Compose smoke job runs the mailer, not the drain. It always drains inline, even when `EVENT_DISPATCH=async`. It is not a daemon; a scheduler can call it again. Use it when the API runs `EVENT_DISPATCH=async`, or after a crash left unpublished rows. On SQLite it creates missing tables first. On Postgres it relies on Alembic. Compose stays on inline dispatch.

## Backlog

`GET /api/v1/outbox/summary` returns:

- `unpublished`: rows not yet published.
- `claimed`: unpublished rows whose lease is still inside the timeout. An expired lease counts as unpublished and not claimed.
- `oldest_unpublished_age_seconds`: whole seconds since the oldest unpublished row was created, or `null` when there are none.

It reads the database with the injected clock, so it does not depend on which process drained last. The gauges `catalogguard_outbox_unpublished` and `catalogguard_outbox_oldest_unpublished_seconds` (0 when nothing is pending) are set at the end of each drain in the API process and whenever the summary is read. The drain worker's gauges disappear when that process exits, so use the summary endpoint for the current backlog.

```bash
make drain
curl -s http://127.0.0.1:8000/api/v1/outbox/summary
```

<details>
<summary>Windows PowerShell</summary>

```powershell
make drain
curl.exe -s http://127.0.0.1:8000/api/v1/outbox/summary
```

</details>

Inline dispatch holds a process-wide lock until the outbox is empty. A stock update for an unrelated SKU waits behind an in-flight model call. That is acceptable for the demo and a poor fit for a burst of store updates. `EVENT_DISPATCH=async` returns after scheduling the drain, and `make drain` can finish what a background drain left behind. Compose still uses inline so the curl walkthrough can read drafts immediately.

Inline dispatch keeps the request open for the LLM call. That is the right demo default and a poor fit once rewrite latency matters. `EVENT_DISPATCH=async` only moves the drain onto a background task. It does not make the drain safe across instances.

## Idempotency

The inventory write, the idempotency record, and the `InventoryChanged` outbox row commit in one transaction. A client retry with the same key returns the stored response and does not apply the write again. If the process dies after that commit and before the drain, the next request's drain (and the drain on startup) delivers the unpublished row. Handlers are safe to run again: threshold evaluation records `processed_events` in the same transaction as the downstream outbox row, listing drafts are unique on `trigger_event_id`, and email drafts are unique on `dedupe_key`.

## Failure modes

A listing or email failure does not roll back the stock count. Malformed model output is retried, then written to `dead_letters`. A validation failure is deterministic, so it is stored on the draft and not retried. The email handler still runs if the listing handler fails. A crossing inside the cooldown does not fire; it is recorded in `suppressed_crossings`. Permanent LLM errors (for example HTTP 400) are not retried inside the provider. Transport errors and HTTP 429/5xx are.

## Recovering a dead letter

`POST /api/v1/dead-letters/{id}/redrive` takes no body and runs only that letter's handler (`threshold_evaluation`, `listing_rewrite`, or `email_draft`) once, on the event stored with the letter.

- **Scope.** It does not go back through the outbox, so the other subscribers for that event do not run again. It does not retry, and it never writes a new dead letter.
- **The letter.** It is kept. Each attempt is a row in `dead_letter_redrives`, and `GET /api/v1/dead-letters` shows `redrive_count`, `last_redrive_status`, and `last_redriven_at`.
- **After a success.** A second POST returns `"result": "replayed"` without running the handler.
- **After a failure.** The handler's error keeps its usual status (429 `llm_cost_limit`, 404, 502 `malformed_llm_output`, or 500). It is recorded as `failed`, and a later POST can try again.
- **Threshold letters.** A threshold redrive writes `LowInventoryDetected` and then drains once, so the listing and email drafts exist when it returns. If that drain fails, the redrive still returns success and the failure is logged.
- **Listing letters.** A listing redrive goes through the normal daily LLM cap. It spends one call when no draft exists yet for that trigger. It spends nothing, and returns the existing draft, when one does.
- **Email letters.** A recovered email draft is `queued` like any other, and a sold-out event still queues none. Nothing is sent, and no handoff is recorded until the handoff worker runs.
- **Refusals.** An `outbox_decode` letter returns 409 `Undecodable outbox rows cannot be redriven`, and the row is not put back on the outbox. A payload that no longer decodes, or a handler name that is not registered, is also 409. None of these refusals records an attempt.
- **One in flight.** Before the handler runs, the redrive commits a row in `dead_letter_redrive_claims` in its own short transaction. A second request for the same letter, from this process or another, gets 409 `A redrive of this dead letter is already in progress` while that claim is younger than `REDRIVE_CLAIM_TIMEOUT_SECONDS` (default 300). It does not call the handler or record an attempt. A claim that old or older can be taken over with a compare-and-set on `claimed_at`. While the handler runs, a keeper refreshes that timestamp every `REDRIVE_CLAIM_RENEW_SECONDS` (default 60), the same idea as the outbox lease, so a slow call does not look idle. The renew interval must be greater than 0 and less than the claim timeout, or startup refuses. The success or failure row is written in the same transaction that releases the claim, and only if the claim is still this attempt's. The remaining hole is a process that dies mid-handler: nothing refreshes the claim, and a later request can take it over. Revision `6e218e07a396` adds the table.

`catalogguard_dead_letter_redrives_total{result}` counts `succeeded`, `failed`, `rejected` (the refusals and unknown ids), `replayed`, and `in_progress` (a live claim held elsewhere). A redrive never increments `catalogguard_dead_letters_total`.

```bash
curl -s "http://127.0.0.1:8000/api/v1/dead-letters"
curl -s -X POST "http://127.0.0.1:8000/api/v1/dead-letters/<dead-letter-id>/redrive"
```

<details>
<summary>Windows PowerShell</summary>

```powershell
curl.exe -s "http://127.0.0.1:8000/api/v1/dead-letters"
curl.exe -s -X POST "http://127.0.0.1:8000/api/v1/dead-letters/<dead-letter-id>/redrive"
```

</details>

## Inventory versus listing consistency

The stock row is the source of truth for quantity. Listing copy is a proposal in `listing_drafts`. Those writes are separate transactions on purpose: a model outage must not block a stock update, and a draft must not change what the storefront shows until someone accepts it. Readers can observe a new quantity before a draft exists when dispatch is async, or when the handler is still retrying. They must not assume the live SEO fields moved. They did not.

## Optimistic locking

`InventoryLevel.version` starts at 1. The client sends `expected_version`. The update is a compare-and-set. A mismatch returns 409 and the current version. The caller re-reads and retries with a new idempotency key if the body changed. Two workers cannot both apply a stale quantity.

## LLM cost

The stub is the default. A real call happens only on a downward crossing outside the cooldown, on a manual rewrite, and only while the SKU is under `LLM_DAILY_CAP_PER_SKU`. The cap is per SKU per UTC day and is shared by every store. Retries spend the same budget. Validation failures are not retried after a parse succeeds. Prompts are versioned, so a prompt edit does not silently rewrite history. Structured output (`json_schema`, strict) is requested so fewer calls die in the parser. Lower the cap before pointing a busy catalog at a paid model.

## PII

Email drafts store customer ids, not addresses, names, or message bodies personalized with contact data. Logs record a recipient count. The listing prompt contains the product and the store only. The handoff worker removes ids on `email_suppressions` before a mailer sees them, and logs counts, not ids. The mailer that later joins these ids to a CRM still has to apply access control, retention, and its own delivery-time suppression, such as bounces or a CRM opt-out that never reached this table. Do not log the draft or handoff payload.

## Kafka and more than one instance

Keep writing the outbox in the state-change transaction. The API drain already claims rows. The next move is a publisher that produces leased rows to `catalogguard.events` with key `{sku}|{store_id}`, then marks `published_at`. Run listing rewrite and email drafts as consumers when that work should leave the API process. The skeleton is `KafkaEventBus` in `infrastructure/events/kafka.py`. It raises if called. The swap is described in `adr/0001-kafka-event-bus.md`. SQS is the same shape with a visibility timeout and a dead-letter queue.

## Migrations

Local SQLite and the test suite create tables with `metadata.create_all` on startup. The API does that only when `DATABASE_URL` is SQLite. Postgres does not run `create_all`, so a new table or column has to be in an Alembic revision or it will be missing in Docker. Docker runs `python -m catalogguard.migrate` (`alembic upgrade head`) before the API listens. `make migrate` does the same against `DATABASE_URL`. Run that once per deploy, not inside every replica if more than one container starts together. The revisions, in order after the initial schema `0d865ba6ff4d`: `15e685dc71f6` (`email_suppressions`, `email_handoffs`), `c81f0f63dcdc` (`dead_letter_redrives`), `5751f906cab6` (`store_listing_revisions`), `89e567ee29fd` (`inventory_changes`), `b19fd5f52b4b` (`suppressed_crossings`), and `6e218e07a396` (`dead_letter_redrive_claims`). The lease renewal needs no schema change. A SQLite file created before `store_listings`, `llm_usage`, or `claimed_at` existed will not pick up those columns from `create_all`. Delete `catalogguard.db` and run `make seed`, or start from an empty database and run `make migrate`.
