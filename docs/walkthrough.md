# Curl walkthrough

Assumes a fresh seed and the API from the [quickstart](../README.md#quickstart). `BKPK-28L-OLV` at `nyc` starts at quantity 12, version 1. The global threshold is 5. Four seeded customers interacted with that pack in New York. `cus_1009` viewed it in Chicago and must not appear on the New York draft.

Set `B` once. Bash is shown first. PowerShell 5.1 strips quotes before they reach native programs, so the PowerShell PUTs use `curl.exe --%`.

```bash
B=http://127.0.0.1:8000
curl -s "$B/health"

curl -s "$B/api/v1/thresholds/BKPK-28L-OLV/nyc"
curl -s "$B/api/v1/thresholds/BLKT-WOL-QUE/nyc"
curl -s "$B/api/v1/thresholds/BLKT-WOL-QUE/aus"

curl -s "$B/api/v1/inventory/BKPK-28L-OLV/nyc"

curl -s -D - -X PUT "$B/api/v1/inventory/BKPK-28L-OLV/nyc" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: walkthrough-bkpk-nyc-1" \
  -H "X-Correlation-ID: walkthrough-1" \
  --data-raw '{"quantity":2,"expected_version":1}'

curl -s "$B/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc"

curl -s "$B/api/v1/email-drafts?sku=BKPK-28L-OLV&store_id=nyc"

curl -s -D - -X PUT "$B/api/v1/inventory/BKPK-28L-OLV/nyc" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: walkthrough-bkpk-nyc-1" \
  --data-raw '{"quantity":2,"expected_version":1}'

curl -s "$B/metrics"
```

<details>
<summary>Windows PowerShell</summary>

`curl.exe` avoids PowerShell's `curl` alias.

```powershell
curl.exe -s http://127.0.0.1:8000/health

curl.exe -s http://127.0.0.1:8000/api/v1/thresholds/BKPK-28L-OLV/nyc
curl.exe -s http://127.0.0.1:8000/api/v1/thresholds/BLKT-WOL-QUE/nyc
curl.exe -s http://127.0.0.1:8000/api/v1/thresholds/BLKT-WOL-QUE/aus

curl.exe -s http://127.0.0.1:8000/api/v1/inventory/BKPK-28L-OLV/nyc

curl.exe -s -D - --% -X PUT "http://127.0.0.1:8000/api/v1/inventory/BKPK-28L-OLV/nyc" -H "Content-Type: application/json" -H "Idempotency-Key: walkthrough-bkpk-nyc-1" -H "X-Correlation-ID: walkthrough-1" --data-raw "{\"quantity\":2,\"expected_version\":1}"

curl.exe -s "http://127.0.0.1:8000/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc"

curl.exe -s "http://127.0.0.1:8000/api/v1/email-drafts?sku=BKPK-28L-OLV&store_id=nyc"

curl.exe -s -D - --% -X PUT "http://127.0.0.1:8000/api/v1/inventory/BKPK-28L-OLV/nyc" -H "Content-Type: application/json" -H "Idempotency-Key: walkthrough-bkpk-nyc-1" --data-raw "{\"quantity\":2,\"expected_version\":1}"

curl.exe -s http://127.0.0.1:8000/metrics
```

</details>

Publish the draft that the crossing just stored.

```bash
DRAFT_ID=$(curl -s "$B/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc" | python -c 'import sys,json; print(json.load(sys.stdin)["items"][0]["id"])')
curl -s -X POST "$B/api/v1/listings/drafts/$DRAFT_ID/publish" \
  -H "Content-Type: application/json" \
  --data-raw '{"expected_version":0}'
curl -s "$B/api/v1/listings/BKPK-28L-OLV/stores/nyc"
```

<details>
<summary>Windows PowerShell</summary>

```powershell
$drafts = Invoke-RestMethod "http://127.0.0.1:8000/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc"
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/listings/drafts/$($drafts.items[0].id)/publish" -ContentType "application/json" -Body '{"expected_version":0}'
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/listings/BKPK-28L-OLV/stores/nyc"
```

</details>

The live title matches the draft. `GET /api/v1/products/BKPK-28L-OLV` still shows the seeded SEO fields.

What you should see:

- Threshold sources: `global` 5 for the pack in New York, `sku` 8 for the wool blanket in New York, `sku_store` 3 for that blanket in Austin.
- The PUT returns quantity 2, previous quantity 12, version 2, and echoes `X-Correlation-ID: walkthrough-1`.
- One listing draft, `status` `ready`, `prompt_version` `listing_rewrite/v1`, `model` `stub-deterministic-v1`, empty `violations`. The title starts with `Fieldwork Supply Trailhead 28 Pack`. The product's live SEO fields are unchanged.
- One email draft, `status` `queued`, subject `Only 2 left in Hudson Market — act now`, recipients `cus_1842` (`add_to_cart`), `cus_4410`, `cus_7781`, and `cus_9021`. Not `cus_1009`.
- The second PUT returns the same body and `Idempotency-Replayed: true`. Draft counts stay at one.
- `/metrics` includes `catalogguard_events_emitted_total`, `catalogguard_listing_rewrites_total`, and `catalogguard_llm_latency_seconds`.

A manual rewrite, which does not queue email:

```bash
curl -s -X POST "$B/api/v1/listings/LANT-LED-01/stores/nyc/rewrite"
```

<details>
<summary>Windows PowerShell</summary>

```powershell
curl.exe -s -X POST "http://127.0.0.1:8000/api/v1/listings/LANT-LED-01/stores/nyc/rewrite"
```

</details>

If this database was already walked through, `expected_version` is no longer 1 and the pack may already be below the threshold. Either delete `catalogguard.db` and run `make seed` again, or read the version and cross again with a new idempotency key:

```bash
curl -s -D - -X PUT "$B/api/v1/inventory/BKPK-28L-OLV/nyc" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: walkthrough-bkpk-nyc-restock" \
  --data-raw '{"quantity":12,"expected_version":2}'

curl -s -D - -X PUT "$B/api/v1/inventory/BKPK-28L-OLV/nyc" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: walkthrough-bkpk-nyc-2" \
  --data-raw '{"quantity":2,"expected_version":3}'
```

<details>
<summary>Windows PowerShell</summary>

```powershell
curl.exe -s -D - --% -X PUT "http://127.0.0.1:8000/api/v1/inventory/BKPK-28L-OLV/nyc" -H "Content-Type: application/json" -H "Idempotency-Key: walkthrough-bkpk-nyc-restock" --data-raw "{\"quantity\":12,\"expected_version\":2}"

curl.exe -s -D - --% -X PUT "http://127.0.0.1:8000/api/v1/inventory/BKPK-28L-OLV/nyc" -H "Content-Type: application/json" -H "Idempotency-Key: walkthrough-bkpk-nyc-2" --data-raw "{\"quantity\":2,\"expected_version\":3}"
```

</details>

Seed is idempotent and will not reset a quantity that is already stored. A second `make seed` does not rewind the demo.

## Hand the queued draft to a mailer

This assumes the walkthrough above has already queued the New York draft, and that nobody is suppressed. Keep the API running. Run the worker from a second terminal in the repository root:

```bash
make handoff
curl -s "$B/api/v1/email-handoffs?sku=BKPK-28L-OLV&store_id=nyc"
make handoff
```

<details>
<summary>Windows PowerShell</summary>

```powershell
make handoff
curl.exe -s "http://127.0.0.1:8000/api/v1/email-handoffs?sku=BKPK-28L-OLV&store_id=nyc"
make handoff
```

</details>

Without `make`, run `python -m catalogguard.mailer` instead (on Windows, `.\.venv\Scripts\python.exe -m catalogguard.mailer`). The worker writes JSON log lines to stderr and a summary to stdout.

What you should see:

- The first run prints `{"examined": 1, "handed_off": 1, "suppressed": 0, "expired": 0, "skipped": 0}`.
- The GET returns one handoff with status `handed_off`, recipients `cus_1842`, `cus_4410`, `cus_7781`, and `cus_9021`, an empty `suppressed_customer_ids`, the draft's `quantity_as_of`, and a `handoff_note`.
- The second run prints `"examined": 0`. Nothing new is recorded.
- `GET /api/v1/email-drafts?sku=BKPK-28L-OLV&store_id=nyc` still shows the draft as `queued`.

Nothing was delivered. No email was sent and no mail provider was called. The handoff row is a record for a mailer that does not exist in this repository.

To see suppression, start from a fresh seed (delete `catalogguard.db`, then `make seed` and `make run`). Suppress a customer before the first crossing PUT, then run the walkthrough PUT and `make handoff`. `cus_1842` is then missing from `recipients` and listed in `suppressed_customer_ids`:

```bash
curl -s -X POST "$B/api/v1/email-suppressions" \
  -H "Content-Type: application/json" \
  --data-raw '{"customer_id":"cus_1842","reason":"unsubscribed"}'
```

<details>
<summary>Windows PowerShell</summary>

```powershell
curl.exe -s --% -X POST "http://127.0.0.1:8000/api/v1/email-suppressions" -H "Content-Type: application/json" --data-raw "{\"customer_id\":\"cus_1842\",\"reason\":\"unsubscribed\"}"
```

</details>

A suppression added after a draft was already decided does not change that decision. Because of the 24-hour cooldown, a second crossing for the same pack and store will not queue a new draft the same day.

## Walkthrough continued: the cap, a dead letter, and a redrive

**Start the server for this part with `LLM_DAILY_CAP_PER_SKU=1`.** Do it as a second run, after the steps above, so those stay copy-pasteable on a default server. This part uses `SKLT-12-BLK` at `nyc`, which the steps above do not touch. The seed has it at quantity 14, version 1, under the global threshold of 5. One New York customer, `cus_2201`, added it to a cart. If you have already run this part on this database, delete `catalogguard.db` and run `make seed` first.

In the server terminal, stop the server (Ctrl+C) and start it again with a cap of one model call per SKU per day:

```bash
LLM_DAILY_CAP_PER_SKU=1 make run
```

<details>
<summary>Windows PowerShell</summary>

```powershell
$env:LLM_DAILY_CAP_PER_SKU = "1"
make run
```

</details>

In the second terminal, spend the one call, hit the cap, then cross the threshold:

```bash
curl -s -X POST "$B/api/v1/listings/SKLT-12-BLK/stores/nyc/rewrite"
curl -s -X POST "$B/api/v1/listings/SKLT-12-BLK/stores/nyc/rewrite"

curl -s -D - -X PUT "$B/api/v1/inventory/SKLT-12-BLK/nyc" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: walkthrough-sklt-nyc-1" \
  --data-raw '{"quantity":3,"expected_version":1}'

curl -s "$B/api/v1/dead-letters"
curl -s "$B/api/v1/email-drafts?sku=SKLT-12-BLK&store_id=nyc"
```

<details>
<summary>Windows PowerShell</summary>

```powershell
curl.exe -s -X POST "http://127.0.0.1:8000/api/v1/listings/SKLT-12-BLK/stores/nyc/rewrite"
curl.exe -s -X POST "http://127.0.0.1:8000/api/v1/listings/SKLT-12-BLK/stores/nyc/rewrite"

curl.exe -s -D - --% -X PUT "http://127.0.0.1:8000/api/v1/inventory/SKLT-12-BLK/nyc" -H "Content-Type: application/json" -H "Idempotency-Key: walkthrough-sklt-nyc-1" --data-raw "{\"quantity\":3,\"expected_version\":1}"

curl.exe -s "http://127.0.0.1:8000/api/v1/dead-letters"
curl.exe -s "http://127.0.0.1:8000/api/v1/email-drafts?sku=SKLT-12-BLK&store_id=nyc"
```

</details>

What you should see:

- The first rewrite returns 201 with a `manual` draft whose `status` is `ready`. That spends the SKU's one call for the UTC day.
- The second rewrite returns 429 with code `llm_cost_limit` and the message `Daily LLM cap of 1 calls reached for SKU SKLT-12-BLK`.
- The PUT returns 200 with quantity 3, previous quantity 14, and version 2. A handler failure does not undo the stock write.
- `GET /api/v1/dead-letters` lists a letter with `handler` `listing_rewrite`, the same daily-cap message as its `error`, and `redrive_count` 0.
- One email draft, `status` `queued`, subject `Only 3 left in Hudson Market — act now`, with one recipient: `cus_2201` (`add_to_cart`).

The crossing fans out to two handlers. The cap stops the listing handler before it calls the model, so that handler dead-letters. The email handler makes no model call and the cap does not gate it, so the email draft is still queued.

Now raise the cap. In the server terminal, stop the server and start it again without `LLM_DAILY_CAP_PER_SKU` (the default is 24):

```bash
make run
```

<details>
<summary>Windows PowerShell</summary>

```powershell
$env:LLM_DAILY_CAP_PER_SKU = "24"
make run
```

</details>

The dead letter is still in `catalogguard.db`. A restart neither clears it nor retries it. In the second terminal, redrive it twice:

```bash
LETTER_ID=$(curl -s "$B/api/v1/dead-letters" | python -c 'import sys,json; items=json.load(sys.stdin)["items"]; print(next(i["id"] for i in items if i["handler"]=="listing_rewrite" and i["payload"].get("sku")=="SKLT-12-BLK"))')
curl -s -X POST "$B/api/v1/dead-letters/$LETTER_ID/redrive"
curl -s -X POST "$B/api/v1/dead-letters/$LETTER_ID/redrive"

curl -s "$B/api/v1/dead-letters"
curl -s "$B/api/v1/listings/drafts?sku=SKLT-12-BLK&store_id=nyc"
curl -s "$B/api/v1/email-drafts?sku=SKLT-12-BLK&store_id=nyc"
```

<details>
<summary>Windows PowerShell</summary>

```powershell
$letter = (Invoke-RestMethod "http://127.0.0.1:8000/api/v1/dead-letters").items | Where-Object { $_.handler -eq "listing_rewrite" -and $_.payload.sku -eq "SKLT-12-BLK" } | Select-Object -First 1
curl.exe -s -X POST "http://127.0.0.1:8000/api/v1/dead-letters/$($letter.id)/redrive"
curl.exe -s -X POST "http://127.0.0.1:8000/api/v1/dead-letters/$($letter.id)/redrive"

curl.exe -s "http://127.0.0.1:8000/api/v1/dead-letters"
curl.exe -s "http://127.0.0.1:8000/api/v1/listings/drafts?sku=SKLT-12-BLK&store_id=nyc"
curl.exe -s "http://127.0.0.1:8000/api/v1/email-drafts?sku=SKLT-12-BLK&store_id=nyc"
```

</details>

What you should see:

- The first POST returns 200 with `"result": "succeeded"` and `"replayed": false`.
- The second POST returns 200 with `"result": "replayed"`, `"replayed": true`, and the same `redrive_id`. The handler did not run again.
- The letter is kept, with `redrive_count` 1 and `last_redrive_status` `succeeded`. A replay records no new attempt, so the count stays at 1.
- Two listing drafts: the earlier `manual` one, and a `low_inventory` draft whose `trigger_event_id` is the letter's `event_id`.
- Still one email draft. The redrive ran only the named `listing_rewrite` handler, not the email handler.

When you are done, unset `LLM_DAILY_CAP_PER_SKU` so the next start uses the `.env` value, or the default of 24. On PowerShell: `Remove-Item Env:LLM_DAILY_CAP_PER_SKU`.
