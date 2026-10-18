# API

Interactive docs are served by the running app at <http://127.0.0.1:8000/docs>.

| Method | Path | Role |
| --- | --- | --- |
| GET | `/health`, `/metrics` | Process health and Prometheus counters |
| POST/GET/PATCH/DELETE | `/api/v1/products` | Product catalog. Delete is rejected while inventory or other rows reference the SKU |
| POST/GET | `/api/v1/stores` | Stores |
| PUT | `/api/v1/inventory/{sku}/{store_id}` | Set quantity. Requires `Idempotency-Key` and `expected_version` (`0` creates the row) |
| GET | `/api/v1/inventory`, `/api/v1/inventory/{sku}/{store_id}` | Read on-hand quantity and version |
| GET | `/api/v1/inventory/{sku}/{store_id}/changes` | Quantity and threshold history for that pair, newest first. Paginated. 404 for an unknown product or store |
| GET | `/api/v1/stores/{store_id}/low-stock` | Rows below their effective threshold, by SKU, with the latest listing and email draft ids and statuses. Paginated. 404 for an unknown store |
| GET | `/api/v1/listings/drafts/{id}/cause` | The inventory write behind a low-inventory draft. `null` for a manual draft or a missing link |
| GET | `/api/v1/suppressed-crossings` | Crossings dropped inside the cooldown, newest first. `store_id` required (404 if unknown), optional `sku`, paginated. No POST or delete |
| GET | `/api/v1/thresholds/{sku}/{store_id}` | Effective threshold and which level supplied it |
| POST | `/api/v1/listings/{sku}/stores/{store_id}/rewrite` | Manual listing draft. Does not queue email |
| GET | `/api/v1/listings/drafts` | Listing drafts |
| POST | `/api/v1/listings/drafts/{id}/publish` | Copy a ready draft onto the live store listing. `expected_version` 0 creates it |
| GET | `/api/v1/listings/{sku}/stores/{store_id}` | Live store listing |
| GET | `/api/v1/listings/{sku}/stores/{store_id}/revisions` | Live listing history, newest version first. Paginated |
| POST | `/api/v1/listings/{sku}/stores/{store_id}/rollback` | Republish an earlier revision through publish. Body `revision_version`, `expected_version` |
| GET | `/api/v1/outbox/summary` | Unpublished rows, live leases, and the oldest unpublished age in seconds |
| GET | `/api/v1/email-drafts` | Queued email drafts |
| POST/GET | `/api/v1/email-suppressions` | Suppress an opaque customer id (`customer_id`, `reason`). Duplicate is 409. GET is paginated |
| GET | `/api/v1/email-handoffs` | Handoff decisions. Filters `status`, `sku`, `store_id`, plus `limit` and `offset`. There is no send route |
| GET | `/api/v1/dead-letters` | Handlers that exhausted retries, with `redrive_count`, `last_redrive_status`, and `last_redriven_at` |
| POST | `/api/v1/dead-letters/{id}/redrive` | Run only that letter's handler on its stored event. No body. The letter is kept. `outbox_decode` is 409. Nothing is sent |

Inventory conflicts:

- `409` `optimistic_conflict` with `current_version` when `expected_version` does not match
- `409` `idempotency_conflict` when the key was already used with a different body
- The same key and the same body return the original response and `Idempotency-Replayed: true`

Omit `threshold_override` to leave the current override alone. Send `null` to clear it.

Inventory history (`GET /api/v1/inventory/{sku}/{store_id}/changes`) is append-only. A row is written in the same transaction as the stock row, only when quantity or the threshold override actually changes, and it carries the same `event_id` as that write's `InventoryChanged`. Replays, no-op writes, and rejected writes add nothing. History is not backfilled: changes made before revision `89e567ee29fd`, and seeded stock, have no rows.

## The low-stock board and the cause link

- **Board:** `GET /api/v1/stores/{store_id}/low-stock` uses the same `resolve_threshold` as the crossing (store override, then SKU override, then the global default) and lists rows where `quantity < threshold`. That includes sold-out rows whose crossing queued no email. It shows only draft ids and statuses, never recipients or bodies. See [thresholds](behavior.md#thresholds).
- **Cause:** `GET /api/v1/listings/drafts/{id}/cause` follows a low-inventory draft's `trigger_event_id` to the `LowInventoryDetected` outbox row. That row's `causation_id` points to the `inventory_changes` row with the same `event_id`. If the outbox row or history row is missing, or belongs to another SKU or store, `inventory_change` is `null` instead of an error. A manual draft has no cause.
