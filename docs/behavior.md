# Behavior

How a low-stock write becomes a draft, a queued email, or a recorded suppression. The sequence diagram is on the [README](../README.md#low-inventory-path).

`EVENT_DISPATCH=inline` (the default) finishes that drain before the HTTP response returns, so a follow-up read sees the drafts. `EVENT_DISPATCH=async` schedules the drain on a background task in the same process. The outbox row is committed either way.

Listing rewrites are store-scoped. The handler inserts a `ListingDraft`. It does not write `Product` SEO fields. An operator publishes a ready draft with `POST /api/v1/listings/drafts/{id}/publish`. That writes `store_listings` for the SKU and store. A draft with status `validation_failed` is rejected. Publishing the same draft again with the live row's `expected_version` returns the current live row without a new version. With a different `expected_version` it is 409. Replacing it requires the live row's `expected_version`.

Every publish that stores a new live version also appends a row to `store_listing_revisions` in the same transaction. That row holds the stripped, customer-facing text that just went live, never the draft markers. `GET /api/v1/listings/{sku}/stores/{store_id}/revisions` lists them, newest version first.

`POST /api/v1/listings/{sku}/stores/{store_id}/rollback` with `{"revision_version": 1, "expected_version": 2}` republishes the draft behind that revision through the same publish step:

- The live version goes up, the text is stripped again, product SEO is untouched, and a new revision records it.
- If the revision is already live, it is 409 `That revision is already live`.
- A stale `expected_version` is 409 `optimistic_conflict`.
- A draft that no longer matches the product's attributes is 409 `conflict`, and the live row does not change.
- If there is no live row or no such revision, it is 404.

There is no unpublish and no delete of revisions. History starts with this feature: a listing published before revision `5751f906cab6` has a live row and no history until its next publish. Nothing is backfilled.

The email handler inserts one `EmailDraft` with status `queued`, including `quantity_as_of`. A quantity of 0 does not queue an "act now" draft. A downstream mailer owns delivery and must read stock again before sending, because the queued number can go stale.

## Thresholds

Effective threshold for a (SKU, store) pair, most specific value that is defined:

1. `InventoryLevel.threshold_override` for that pair
2. `Product.threshold_override` for the SKU
3. `DEFAULT_LOW_STOCK_THRESHOLD` (default 5)

`None` means "not set". A stored `0` is set, and it wins over a broader value.

Low stock means `quantity < threshold`. Threshold 5 alerts at 4 or fewer. Quantity equal to the threshold does not.

## Crossing and cooldown

A low-stock event fires only on a downward crossing: previous quantity was at or above the threshold, and the new quantity is strictly below it.

- The first insert is not a crossing, even when the quantity is already low. Seed data can sit below threshold without creating drafts.
- Moving from 4 to 2 while the threshold is 5 is not a new crossing.
- After an event fires, another crossing for that (SKU, store) inside `LOW_STOCK_COOLDOWN_SECONDS` (default 24 hours) is dropped. It is not deferred.

Trade-off: a real restock-and-sellout inside the window is silent, and a crossing during the window is gone even if the item stays low. That stops a flapping count (5 → 4 → 6 → 4) from calling the model and queueing mail on every wobble. The manual rewrite endpoint is the operator path. Cooldown is judged from the event timestamp, so a redelivered inventory event does not extend the window or fire twice.

A crossing inside the window is stored in `suppressed_crossings` with `reason=cooldown`, the quantities, the effective threshold and its source, and `cooldown_until`. `GET /api/v1/suppressed-crossings?store_id=…` lists them, newest first. A stored suppression is not retried when the window ends, and the cooldown clock does not move. A move that stays below the threshold, a first insert, and a crossing at the exact end of the window are not suppressions. Rows start with revision `b19fd5f52b4b`; earlier suppressions were only log lines.

## Email targeting

This is a deliberate simplification. `RecentInteraction` stores `customer_id`, `sku`, `store_id`, `interaction_type` (`view` or `add_to_cart`), and `timestamp`. The handler loads rows for that SKU and store in the lookback window (`EMAIL_LOOKBACK_HOURS`, default 168), keeps one row per customer, and prefers `add_to_cart` over `view`. The payload carries opaque customer ids. This service does not resolve names or email addresses.

The email body is rendered from `prompts/email_urgency/v1.md`. It is not sent to the LLM, so customer ids never leave this database toward a model.

## Email handoff

CatalogGuard still sends nothing. `python -m catalogguard.mailer` (or `make handoff`) decides which queued drafts are ready for a downstream mailer and writes one `email_handoffs` row per draft. It runs one batch and exits. It is not a daemon. A scheduler such as cron or a Kubernetes CronJob can call it again.

Each run takes up to `EMAIL_HANDOFF_BATCH_SIZE` (default 50) drafts that are still `queued` and have no handoff row, oldest first. For each draft, in order:

1. Older than `EMAIL_HANDOFF_MAX_AGE_HOURS` (default 72, strictly greater): status `expired`. The payload names the draft id, SKU, and store and says it aged out. It carries no recipients.
2. Otherwise the draft payload is copied, and every recipient whose `customer_id` is in `email_suppressions` is removed. The removed ids go on `suppressed_customer_ids`. Matching is exact on the opaque id.
3. Nobody left, including a draft that never had recipients: status `suppressed`, `recipients` empty, subject and body kept, and a `handoff_note` that says why.
4. At least one recipient left: status `handed_off`. The payload keeps the draft fields (`subject`, `body`, `template_version`, `quantity_remaining`, `quantity_as_of`, `sku`, `store_id`, `store_name`, `product_name`, `targeting`, `targeting_note`), lists only the remaining recipients, and adds a `handoff_note` saying it was not sent.

The handoff row is the decision and the idempotency record. `email_draft_id` is unique. A second run skips drafts that already have a row. When two workers pick the same draft, the loser's insert hits the unique constraint inside a savepoint, the worker skips that draft, and the first decision stays as it was. A suppression added after a decision does not change that decision. The email draft itself stays `queued`, and a sold-out crossing that never created a draft never gets a handoff.

Suppressions are opaque customer ids (`^[A-Za-z0-9_.:-]{1,64}$`, so an email address is rejected) with a required reason of up to 200 characters. There is no delete endpoint yet.

The worker logs `email_handoff_recorded` with the draft id, SKU, store, status, remaining recipient count, and suppressed count. It never logs the ids. It increments `catalogguard_email_handoffs_total{result}` once per new decision (`handed_off`, `suppressed`, `expired`). That counter lives in the worker process, which exits after one batch, so the API's `/metrics` does not show it. Read the JSON summary that the worker prints as its last line, or count `email_handoffs` rows.

On SQLite the worker creates missing tables first, like the API. On Postgres it relies on Alembic, so run `make migrate` first. The drain worker (`make drain`, in [operations](operations.md#draining-without-the-api)) follows the same rule.

## Listing guardrail

`prompts/listing_rewrite/v1.md` is the prompt. Every draft stores `prompt_version` (`listing_rewrite/v1`) and the model name.

The model must return JSON. The service checks:

- required fields are present and non-empty
- title ≤ 70 characters, meta description ≤ 160, plus the other limits in `domain/listing_rules.py`
- every `cited_attributes` entry exists on the product
- every cited key appears in the copy as `[[key]]`
- the attribute's value appears as its own token

Violations are stored on the draft with status `validation_failed`. The draft is not treated as ready. Each `[[key]]` must have that attribute's value in the 48 characters just before the marker, so a value that only appears in the product name does not satisfy a citation. This is still not a proof that free prose is entailed by the catalog. Uncited claims can slip through. The cases in `evals/listing_guardrail.json` lock those rules.

Drafts keep the markers. Publish copies the text onto `store_listings` with the markers removed, after checking the draft against the product attributes as they are now. A catalog edit between generation and publish blocks publish. `X-Correlation-ID` longer than 64 characters is rejected, because that is the column width on Postgres.

Each SKU may spend `LLM_DAILY_CAP_PER_SKU` generations per UTC day (default 24). `0` turns the cap off. The reservation is counted before the provider is called, including retries, so a failing call still spends budget. A manual rewrite over the cap returns 429 `llm_cost_limit`. The low-stock handler records a dead letter and still queues the email draft. The counter is `catalogguard_listing_rewrites_total{result="cost_limited"}`.

The running default is a deterministic stub (`stub-deterministic-v1`), so the service and the tests need no API key. Set `LLM_PROVIDER=openai` to call any OpenAI-compatible Chat Completions server: set `LLM_BASE_URL` and `LLM_MODEL`. The configured example is xAI at `https://api.x.ai/v1`, model `grok-4.7` (listed in the xAI docs as of October 2026), using `response_format.type = json_schema`. The key is `LLM_API_KEY` or `XAI_API_KEY`.
