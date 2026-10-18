# CatalogGuard manual regression

Follow this on Windows PowerShell 5.1 in `C:\Users\kiran\PycharmProjects\CatalogGuard`. The Python environment is `.venv` (Python 3.12). `make` uses that interpreter when it exists.

This pass proves the demo on SQLite. Do not install Docker. Do not run `docker compose`. Do not set `LLM_PROVIDER=openai` and do not call a real model. Do not push, do not add a remote, and do not commit unless the user asks. Do not edit application code while testing. Stop the API process when you finish. Do not leave it running.

Record each step as pass or fail, with the status code and the field that was wrong. Stop the manual section on the first failed expectation and report it. The automated section can finish even if a later manual step fails, but say which step failed.

PowerShell 5.1 strips quotes before they reach `curl.exe`. Inventory PUTs in this document use `curl.exe --%`. Read email subjects with `curl.exe`, not `Invoke-RestMethod`. PowerShell misreads the em dash in `Only N left…` because the JSON response has no charset.

## 0. Automated regression

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m ruff format --check src tests
```

Pass: pytest exits 0 and the summary says all passed. Ruff check prints `All checks passed!`. Format check prints that all files are already formatted. Any failure is a stop for the automated section. Paste the failing test name.

## 1. Fresh database and server

Stop anything already listening on port 8000. Then:

```powershell
Remove-Item Env:LLM_DAILY_CAP_PER_SKU -ErrorAction SilentlyContinue
Remove-Item catalogguard.db, catalogguard.db-wal, catalogguard.db-shm -ErrorAction SilentlyContinue
make seed
make run
```

Leave `make run` in that terminal. Use a second terminal for every request below. Base URL is `http://127.0.0.1:8000`.

Pass for seed: the command exits 0. Pass for the server: the next health check succeeds. If port 8000 is already taken, stop that process instead of picking another port.

## 2. Health, thresholds, and the quiet seed

```powershell
curl.exe -s http://127.0.0.1:8000/health
curl.exe -s http://127.0.0.1:8000/api/v1/thresholds/BKPK-28L-OLV/nyc
curl.exe -s http://127.0.0.1:8000/api/v1/thresholds/BLKT-WOL-QUE/nyc
curl.exe -s http://127.0.0.1:8000/api/v1/thresholds/BLKT-WOL-QUE/aus
curl.exe -s http://127.0.0.1:8000/api/v1/inventory/BKPK-28L-OLV/nyc
curl.exe -s "http://127.0.0.1:8000/api/v1/stores/chi/low-stock"
curl.exe -s "http://127.0.0.1:8000/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc"
curl.exe -s "http://127.0.0.1:8000/api/v1/email-drafts?sku=BKPK-28L-OLV&store_id=nyc"
```

Expect:

- Health includes `"llm_provider":"stub"` and `"event_dispatch":"inline"`.
- Pack in New York: threshold 5, source `global`.
- Blanket in New York: threshold 8, source `sku`.
- Blanket in Austin: threshold 3, source `sku_store`.
- Pack inventory: quantity 12, version 1.
- Chicago low-stock includes `BKPK-28L-OLV` at quantity 4, threshold 5, source `global`, and `BLKT-WOL-QUE` at quantity 6, threshold 8, source `sku`. Both have null latest draft ids. A first insert is not a crossing, so seed does not create drafts.
- New York pack listing drafts: empty `items`.
- New York pack email drafts: empty `items`.

## 3. Crossing, cause, and unchanged product SEO

```powershell
curl.exe -s -D - --% -X PUT "http://127.0.0.1:8000/api/v1/inventory/BKPK-28L-OLV/nyc" -H "Content-Type: application/json" -H "Idempotency-Key: walkthrough-bkpk-nyc-1" -H "X-Correlation-ID: walkthrough-1" --data-raw "{\"quantity\":2,\"expected_version\":1}"
curl.exe -s "http://127.0.0.1:8000/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc"
curl.exe -s "http://127.0.0.1:8000/api/v1/email-drafts?sku=BKPK-28L-OLV&store_id=nyc"
curl.exe -s http://127.0.0.1:8000/api/v1/products/BKPK-28L-OLV
curl.exe -s "http://127.0.0.1:8000/api/v1/inventory/BKPK-28L-OLV/nyc/changes"
```

Expect the PUT: HTTP 200, quantity 2, previous quantity 12, version 2, header `x-correlation-id: walkthrough-1`.

Expect one listing draft: `status` `ready`, `prompt_version` `listing_rewrite/v1`, `model` `stub-deterministic-v1`, `violations` empty. The title starts with `Fieldwork Supply Trailhead 28 Pack`. The short description still contains `[[capacity_l]]` and `[[color]]`.

Expect one email draft: `status` `queued`. Read the subject with `curl.exe`. It is `Only 2 left in Hudson Market — act now`. Recipients are `cus_1842` (`add_to_cart`), `cus_4410`, `cus_7781`, and `cus_9021`. `cus_1009` is absent.

Product `seo_title` is still `Trailhead 28 Pack | Fieldwork`.

History has a newest row with `previous_quantity` 12, `new_quantity` 2, `inventory_version` 2.

Take the draft id from the listing response. Then:

```powershell
curl.exe -s "http://127.0.0.1:8000/api/v1/listings/drafts/DRAFT_ID/cause"
```

Expect `reason` `low_inventory`, a non-null `low_stock_event_id`, and `inventory_change.new_quantity` 2. That `event_id` is not the same string as `low_stock_event_id`.

## 4. Replay, conflict, and stale version

```powershell
curl.exe -s -D - --% -X PUT "http://127.0.0.1:8000/api/v1/inventory/BKPK-28L-OLV/nyc" -H "Content-Type: application/json" -H "Idempotency-Key: walkthrough-bkpk-nyc-1" --data-raw "{\"quantity\":2,\"expected_version\":1}"
curl.exe -s --% -X PUT "http://127.0.0.1:8000/api/v1/inventory/BKPK-28L-OLV/nyc" -H "Content-Type: application/json" -H "Idempotency-Key: walkthrough-bkpk-nyc-1" --data-raw "{\"quantity\":3,\"expected_version\":1}"
curl.exe -s --% -X PUT "http://127.0.0.1:8000/api/v1/inventory/BKPK-28L-OLV/nyc" -H "Content-Type: application/json" -H "Idempotency-Key: walkthrough-stale-1" --data-raw "{\"quantity\":3,\"expected_version\":1}"
curl.exe -s "http://127.0.0.1:8000/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc"
```

Expect:

- The first PUT is 200, body matches the original write, and the header `idempotency-replayed` is `true`.
- The second PUT is 409 `idempotency_conflict`.
- The third PUT is 409 `optimistic_conflict` with `current_version` 2.
- There is still exactly one listing draft for the pack in New York.

## 5. Publish, guardrail, and rollback

```powershell
$drafts = Invoke-RestMethod "http://127.0.0.1:8000/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc"
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/listings/drafts/$($drafts.items[0].id)/publish" -ContentType "application/json" -Body '{"expected_version":0}'
curl.exe -s "http://127.0.0.1:8000/api/v1/listings/BKPK-28L-OLV/stores/nyc"
curl.exe -s "http://127.0.0.1:8000/api/v1/listings/BKPK-28L-OLV/stores/nyc/revisions"
curl.exe -s http://127.0.0.1:8000/api/v1/products/BKPK-28L-OLV
```

Expect live version 1. The live `short_description` does not contain `[[`. The draft from step 3 still contains `[[` if you fetch it again. One revision, version 1. Product `seo_title` is still `Trailhead 28 Pack | Fieldwork`.

Publish that same draft again at `expected_version` 1. Expect version 1 still, and still one revision.

```powershell
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/listings/drafts/$($drafts.items[0].id)/publish" -ContentType "application/json" -Body '{"expected_version":1}'
curl.exe -s "http://127.0.0.1:8000/api/v1/listings/BKPK-28L-OLV/stores/nyc/revisions"
```

Guardrail: the lantern draft cites 300 lumens. Change the catalog, then publish must fail and the live lantern listing must stay absent.

```powershell
curl.exe -s -X POST "http://127.0.0.1:8000/api/v1/listings/LANT-LED-01/stores/nyc/rewrite"
```

Save that draft id. Then:

```powershell
curl.exe -s --% -X PATCH "http://127.0.0.1:8000/api/v1/products/LANT-LED-01" -H "Content-Type: application/json" --data-raw "{\"attributes\":{\"lumens\":350,\"runtime_h\":40,\"power\":\"usb-c\"}}"
curl.exe -s -D - --% -X POST "http://127.0.0.1:8000/api/v1/listings/drafts/LANTERN_DRAFT_ID/publish" -H "Content-Type: application/json" --data-raw "{\"expected_version\":0}"
curl.exe -s -D - "http://127.0.0.1:8000/api/v1/listings/LANT-LED-01/stores/nyc"
```

Expect publish 409 `conflict` and a message that includes `value_not_in_text:lumens`. The live GET is 404.

Rollback of the pack listing to the version that is already live:

```powershell
curl.exe -s -D - --% -X POST "http://127.0.0.1:8000/api/v1/listings/BKPK-28L-OLV/stores/nyc/rollback" -H "Content-Type: application/json" --data-raw "{\"revision_version\":1,\"expected_version\":1}"
```

Expect 409 `conflict` and the message `That revision is already live`.

## 6. Handoff

Keep the API running. In a second terminal at the repository root:

```powershell
make handoff
curl.exe -s "http://127.0.0.1:8000/api/v1/email-handoffs?sku=BKPK-28L-OLV&store_id=nyc"
curl.exe -s "http://127.0.0.1:8000/api/v1/email-drafts?sku=BKPK-28L-OLV&store_id=nyc"
make handoff
```

Expect the first summary JSON to include `"examined": 1` and `"handed_off": 1`. The handoff status is `handed_off`. Recipients are the four New York ids from step 3. `suppressed_customer_ids` is empty. `quantity_as_of` is present. The email draft is still `queued`. The second run includes `"examined": 0`.

There is no send route. Do not look for an SMTP call.

## 7. Suppressed crossing

The pack in New York is at quantity 2, which is already below the threshold. Restock, then cross again inside the cooldown. That second drop must not create another listing draft.

Read the current version first (`GET /api/v1/inventory/BKPK-28L-OLV/nyc`). Use that version in the body below if it is not 2.

```powershell
curl.exe -s -D - --% -X PUT "http://127.0.0.1:8000/api/v1/inventory/BKPK-28L-OLV/nyc" -H "Content-Type: application/json" -H "Idempotency-Key: reg-bkpk-restock" --data-raw "{\"quantity\":12,\"expected_version\":2}"
curl.exe -s -D - --% -X PUT "http://127.0.0.1:8000/api/v1/inventory/BKPK-28L-OLV/nyc" -H "Content-Type: application/json" -H "Idempotency-Key: reg-bkpk-cross-again" --data-raw "{\"quantity\":2,\"expected_version\":3}"
curl.exe -s "http://127.0.0.1:8000/api/v1/suppressed-crossings?store_id=nyc&sku=BKPK-28L-OLV"
curl.exe -s "http://127.0.0.1:8000/api/v1/listings/drafts?sku=BKPK-28L-OLV&store_id=nyc"
```

Expect both PUTs to return 200. The suppressed list has one row: `previous_quantity` 12, `new_quantity` 2, `reason` `cooldown`. Listing drafts for that pair are still exactly one.

`GET /api/v1/suppressed-crossings` with no `store_id` is 422. `GET /api/v1/suppressed-crossings?store_id=missing` is 404.

## 8. Cap, dead letter, and redrive

Stop the server in the first terminal (Ctrl+C). Start it again with a cap of one:

```powershell
$env:LLM_DAILY_CAP_PER_SKU = "1"
make run
```

`SKLT-12-BLK` at `nyc` is still the seed row if you have not updated it: quantity 14, version 1. One New York customer, `cus_2201`, added it to a cart.

```powershell
curl.exe -s -D - -X POST "http://127.0.0.1:8000/api/v1/listings/SKLT-12-BLK/stores/nyc/rewrite"
curl.exe -s -D - -X POST "http://127.0.0.1:8000/api/v1/listings/SKLT-12-BLK/stores/nyc/rewrite"
curl.exe -s -D - --% -X PUT "http://127.0.0.1:8000/api/v1/inventory/SKLT-12-BLK/nyc" -H "Content-Type: application/json" -H "Idempotency-Key: walkthrough-sklt-nyc-1" --data-raw "{\"quantity\":3,\"expected_version\":1}"
curl.exe -s "http://127.0.0.1:8000/api/v1/dead-letters"
curl.exe -s "http://127.0.0.1:8000/api/v1/email-drafts?sku=SKLT-12-BLK&store_id=nyc"
```

Expect:

- First rewrite: 201, `reason` `manual`, `status` `ready`.
- Second rewrite: 429, code `llm_cost_limit`, message `Daily LLM cap of 1 calls reached for SKU SKLT-12-BLK`.
- PUT: 200, quantity 3, previous quantity 14, version 2.
- One dead letter, handler `listing_rewrite`, `redrive_count` 0, error text equal to that cap message.
- One email draft, `queued`, one recipient `cus_2201` with `add_to_cart`. Subject, via `curl.exe`: `Only 3 left in Hudson Market — act now`.

Stop the server. Raise the cap. Start again. The SQLite file keeps the dead letter.

```powershell
$env:LLM_DAILY_CAP_PER_SKU = "24"
make run
```

```powershell
$letter = (Invoke-RestMethod "http://127.0.0.1:8000/api/v1/dead-letters").items | Where-Object { $_.handler -eq "listing_rewrite" -and $_.payload.sku -eq "SKLT-12-BLK" } | Select-Object -First 1
curl.exe -s -D - -X POST "http://127.0.0.1:8000/api/v1/dead-letters/$($letter.id)/redrive"
curl.exe -s -D - -X POST "http://127.0.0.1:8000/api/v1/dead-letters/$($letter.id)/redrive"
curl.exe -s "http://127.0.0.1:8000/api/v1/dead-letters"
curl.exe -s "http://127.0.0.1:8000/api/v1/listings/drafts?sku=SKLT-12-BLK&store_id=nyc"
curl.exe -s "http://127.0.0.1:8000/api/v1/email-drafts?sku=SKLT-12-BLK&store_id=nyc"
```

Expect the first redrive: 200, `"result": "succeeded"`, `"replayed": false`. The second: 200, `"result": "replayed"`, `"replayed": true`, same `redrive_id`. The letter has `redrive_count` 1 and `last_redrive_status` `succeeded`. A replay does not add a second attempt. Two listing drafts exist: one `manual`, one `low_inventory`. Still one email draft.

## 9. Small negative checks

```powershell
curl.exe -s -D - -H "X-Correlation-ID: 12345678901234567890123456789012345678901234567890123456789012345" http://127.0.0.1:8000/health
curl.exe -s -D - --% -X POST "http://127.0.0.1:8000/api/v1/email-suppressions" -H "Content-Type: application/json" --data-raw "{\"customer_id\":\"someone@example.com\",\"reason\":\"no\"}"
curl.exe -s http://127.0.0.1:8000/metrics
curl.exe -s http://127.0.0.1:8000/api/v1/outbox/summary
```

Expect:

- Correlation id of 65 characters: 400, code `invalid_request`.
- Suppression of an email-shaped id: 422.
- `/metrics` includes `catalogguard_events_emitted_total` and `catalogguard_listing_rewrites_total`.
- Outbox summary is JSON with `unpublished`, `claimed`, and `oldest_unpublished_age_seconds`. Under inline dispatch the unpublished count is often 0. Do not fail the run only because it is 0.

## 10. Stop

In the server terminal, press Ctrl+C. Then:

```powershell
Remove-Item Env:LLM_DAILY_CAP_PER_SKU -ErrorAction SilentlyContinue
```

Confirm nothing is still listening on port 8000.

## Out of this pass

Do not run Docker or Postgres. Do not call a real LLM. Do not run two API processes, or the API plus `make drain`, against this SQLite file. Do not treat a red GitHub Action as part of this local pass. That check happens after the private push.

## Report

End with a table: step number, pass or fail, and one line of evidence. If everything passed, say the local SQLite demo is ready to push. If something failed, quote the command and the response body, and do not say the demo is ready.
