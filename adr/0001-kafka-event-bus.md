# ADR 0001: Swap the in-process outbox bus for Kafka

## Status

Accepted as the deployment path. The Kafka class in this repo is a skeleton and is not wired.

## Context

Inventory changes, threshold evaluation, listing rewrites, and email drafts are connected by domain events. The running process writes each event to `event_outbox` in the same database transaction as the state change that caused it, then drains that table.

`InProcessEventBus` leases each outbox row (`claimed_at`, `claim_token`) before dispatch. On Postgres the lease is `SELECT … FOR UPDATE SKIP LOCKED`, so several API processes can drain one database without taking the same row. A lease older than `OUTBOX_CLAIM_TIMEOUT_SECONDS` can be claimed again after a crash. The asyncio lock only stops one process from draining itself twice. `EVENT_DISPATCH=async` only moves that drain onto a background task. Kafka is still the path when listing and email work should leave the API process.

Handlers are already safe to run more than once. Listing drafts are unique on `trigger_event_id`. Email drafts are unique on `dedupe_key`. Threshold evaluation records `processed_events` in the same transaction as the downstream outbox row.

## Decision

Keep the `EventBus` port (`publish` into the caller's transaction, `drain` after commit). Replace the drain with a dedicated worker when more than one instance handles traffic.

1. Leave the outbox write in the inventory and threshold transactions. Do not publish to the broker from inside the request before commit.
2. Run one or more outbox publishers that claim rows with `SELECT … FOR UPDATE SKIP LOCKED` on Postgres, publish to topic `catalogguard.events`, then set `published_at`.
3. Use the message key `{sku}|{store_id}` so events for one inventory row stay ordered.
4. A consumer group dispatches on `event_type`. Put listing rewrite and email draft in separate consumer groups if they need to scale independently. Keep threshold evaluation ordered with inventory changes, or fold it into the publisher's first consumer.
5. Retries stay in the consumer. After `HANDLER_MAX_RETRIES`, write `dead_letters` and commit the offset. Do not block the partition on a permanent failure.
6. `KafkaEventBus` in `infrastructure/events/kafka.py` shows the method shape and the topic contract. Wiring a client (for example `aiokafka`) is the implementation step. The skeleton raises rather than pretending to send.

SQS is the same shape: one queue per consumer concern, outbox publisher, visibility timeout longer than handler runtime, and a dead-letter queue in place of the table if the broker owns it. Prefer keeping `dead_letters` as well so operators can query it next to the drafts.

## Consequences

- The API can scale out without double-applying an inventory write. Idempotency keys already cover client retries.
- Listing and email work can lag inventory. That lag is intentional: a failed rewrite must not roll back a stock count.
- The cooldown decision uses the event timestamp, so a redelivered `InventoryChanged` does not extend the cooldown or fire twice.
- Operators need a broker, a publisher deployment, and a consumer deployment. `docker compose` in this repo does not start them.
