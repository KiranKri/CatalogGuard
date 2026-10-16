import json

from tests.fakes import FixedLLM
from tests.support import create_product, create_store

from catalogguard.application.commands import GenerateListing


def _invalid() -> str:
    return json.dumps(
        {
            "title": "T" * 80,
            "short_description": "Waterproof [[waterproof_rating]].",
            "long_description": "Invented claim [[waterproof_rating]].",
            "meta_title": "Meta",
            "meta_description": "Desc",
            "cited_attributes": ["waterproof_rating"],
        }
    )


async def test_stub_rewrite_is_idempotent_for_the_same_event(harness) -> None:
    client, app = await harness()
    await create_store(client)
    await create_product(client)
    command = GenerateListing(
        sku="BKPK-28L-OLV",
        store_id="nyc",
        trigger_event_id="evt-1",
        correlation_id="corr-1",
        reason="manual",
    )
    first = await app.state.container.listings.generate(command)
    second = await app.state.container.listings.generate(command)
    assert first.id == second.id
    assert first.status == "ready"
    assert first.violations == []
    assert first.prompt_version == "listing_rewrite/v1"
    assert first.model == "stub-deterministic-v1"
    assert "[[color]]" in first.short_description
    product = (await client.get("/api/v1/products/BKPK-28L-OLV")).json()
    assert product["seo_title"] == "Live title"
    drafts = (await client.get("/api/v1/listings/drafts")).json()
    assert len(drafts["items"]) == 1


async def test_validation_failure_is_stored_and_not_dead_lettered(harness) -> None:
    llm = FixedLLM(_invalid())
    client, _app = await harness(llm=llm)
    await create_store(client)
    await create_product(client)
    response = await client.post("/api/v1/listings/BKPK-28L-OLV/stores/nyc/rewrite")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "validation_failed"
    assert any(item.startswith("max_length:title") for item in body["violations"])
    assert "unknown_attribute:waterproof_rating" in body["violations"]
    assert llm.calls == 1
    product = (await client.get("/api/v1/products/BKPK-28L-OLV")).json()
    assert product["seo_title"] == "Live title"
    letters = (await client.get("/api/v1/dead-letters")).json()
    assert letters["items"] == []


async def test_malformed_output_retries_then_dead_letters(harness) -> None:
    llm = FixedLLM("this is not json")
    client, _app = await harness(llm=llm, handler_max_retries=3)
    await create_store(client)
    await create_product(client)
    created = await client.put(
        "/api/v1/inventory/BKPK-28L-OLV/nyc",
        json={"quantity": 10, "expected_version": 0},
        headers={"Idempotency-Key": "malformed-create-01"},
    )
    assert created.status_code == 201, created.text
    dropped = await client.put(
        "/api/v1/inventory/BKPK-28L-OLV/nyc",
        json={"quantity": 2, "expected_version": 1},
        headers={"Idempotency-Key": "malformed-drop-0001"},
    )
    assert dropped.status_code == 200, dropped.text
    assert llm.calls == 3
    drafts = (await client.get("/api/v1/listings/drafts")).json()
    assert drafts["items"] == []
    letters = (await client.get("/api/v1/dead-letters")).json()["items"]
    assert [item["handler"] for item in letters] == ["listing_rewrite"]
    emails = (await client.get("/api/v1/email-drafts")).json()
    assert len(emails["items"]) == 1
    assert emails["items"][0]["status"] == "queued"
