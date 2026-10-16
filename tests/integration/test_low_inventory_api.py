"""Full low-inventory path through HTTP, using the stub provider and seed data."""

from catalogguard.seed import seed

HERO = "BKPK-28L-OLV"
RECIPIENTS = {"cus_1842", "cus_9021", "cus_4410", "cus_7781"}


async def test_low_inventory_creates_listing_and_email_drafts(harness) -> None:
    client, app = await harness()
    await seed(app.state.session_factory, now=app.state.clock.now())

    before = await client.get(f"/api/v1/products/{HERO}")
    assert before.json()["seo_title"] == "Trailhead 28 Pack | Fieldwork"
    inventory = await client.get(f"/api/v1/inventory/{HERO}/nyc")
    assert inventory.json()["quantity"] == 12
    assert inventory.json()["version"] == 1

    threshold = await client.get(f"/api/v1/thresholds/{HERO}/nyc")
    assert threshold.json()["effective_threshold"] == 5
    assert threshold.json()["source"] == "global"

    dropped = await client.put(
        f"/api/v1/inventory/{HERO}/nyc",
        json={"quantity": 2, "expected_version": 1},
        headers={
            "Idempotency-Key": "walkthrough-bkpk-nyc-1",
            "X-Correlation-ID": "walkthrough-1",
        },
    )
    assert dropped.status_code == 200, dropped.text
    assert dropped.headers["x-correlation-id"] == "walkthrough-1"
    assert dropped.json()["quantity"] == 2
    assert dropped.json()["previous_quantity"] == 12
    assert dropped.json()["version"] == 2

    drafts = await client.get(f"/api/v1/listings/drafts?sku={HERO}&store_id=nyc")
    assert drafts.status_code == 200
    items = drafts.json()["items"]
    assert len(items) == 1
    draft = items[0]
    assert draft["status"] == "ready"
    assert draft["violations"] == []
    assert draft["prompt_version"] == "listing_rewrite/v1"
    assert draft["model"] == "stub-deterministic-v1"
    assert draft["reason"] == "low_inventory"
    assert draft["version"] == 1
    assert "color" in draft["cited_attributes"]
    assert "[[color]]" in draft["short_description"]
    assert len(draft["title"]) <= 70
    assert len(draft["meta_description"]) <= 160

    emails = await client.get(f"/api/v1/email-drafts?sku={HERO}&store_id=nyc")
    assert len(emails.json()["items"]) == 1
    email = emails.json()["items"][0]
    assert email["status"] == "queued"
    assert email["dedupe_key"] == f"low-stock:{HERO}:nyc:{draft['trigger_event_id']}"
    payload = email["payload"]
    assert payload["subject"] == "Only 2 left in Hudson Market — act now"
    assert payload["template_version"] == "email_urgency/v1"
    assert payload["quantity_remaining"] == 2
    recipients = payload["recipients"]
    assert {row["customer_id"] for row in recipients} == RECIPIENTS
    by_id = {row["customer_id"]: row["interaction_type"] for row in recipients}
    assert by_id["cus_1842"] == "add_to_cart"
    assert "cus_1009" not in by_id

    after = await client.get(f"/api/v1/products/{HERO}")
    assert after.json()["seo_title"] == before.json()["seo_title"]
    assert after.json()["base_description"] == before.json()["base_description"]
    assert after.json()["meta_description"] == before.json()["meta_description"]

    replay = await client.put(
        f"/api/v1/inventory/{HERO}/nyc",
        json={"quantity": 2, "expected_version": 1},
        headers={"Idempotency-Key": "walkthrough-bkpk-nyc-1"},
    )
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json() == dropped.json()
    again = await client.get(f"/api/v1/listings/drafts?sku={HERO}&store_id=nyc")
    assert len(again.json()["items"]) == 1

    metrics = await client.get("/metrics")
    assert metrics.status_code == 200
    text = metrics.text
    assert "catalogguard_events_emitted_total" in text
    assert 'catalogguard_listing_rewrites_total{result="succeeded"}' in text
    assert "catalogguard_llm_latency_seconds" in text
    assert 'catalogguard_email_drafts_total{result="queued"}' in text


async def test_manual_rewrite_does_not_queue_email(harness) -> None:
    client, _app = await harness()
    await client.post(
        "/api/v1/stores",
        json={"id": "nyc", "name": "Hudson Market", "region": "New York"},
    )
    await client.post(
        "/api/v1/products",
        json={
            "sku": "LANT-LED-01",
            "name": "Camp Lantern 300",
            "brand": "Fieldwork Supply",
            "category": "Lighting",
            "attributes": {"lumens": 300, "power": "usb-c"},
            "base_description": "A rechargeable lantern.",
        },
    )
    first = await client.post("/api/v1/listings/LANT-LED-01/stores/nyc/rewrite")
    second = await client.post("/api/v1/listings/LANT-LED-01/stores/nyc/rewrite")
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["status"] == "ready"
    assert first.json()["reason"] == "manual"
    assert second.json()["version"] == first.json()["version"] + 1
    emails = await client.get("/api/v1/email-drafts?sku=LANT-LED-01&store_id=nyc")
    assert emails.json()["items"] == []
