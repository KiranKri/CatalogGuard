"""Email handoff worker: suppression, expiry, idempotency, and the HTTP read side."""

import asyncio
import json
from datetime import timedelta

import pytest
from tests.support import put_inventory

from catalogguard.application.email_handoff import HandoffBatchResult
from catalogguard.infrastructure.config import Settings
from catalogguard.infrastructure.metrics import REGISTRY
from catalogguard.mailer import main, run_once
from catalogguard.seed import seed

HERO = "BKPK-28L-OLV"
RECIPIENTS = {"cus_1842", "cus_9021", "cus_4410", "cus_7781"}
COPIED_FIELDS = (
    "subject",
    "body",
    "template_version",
    "quantity_remaining",
    "quantity_as_of",
    "sku",
    "store_id",
    "store_name",
    "product_name",
    "targeting",
    "targeting_note",
)


def _decisions(result: str) -> float:
    value = REGISTRY.get_sample_value("catalogguard_email_handoffs_total", {"result": result})
    return value or 0.0


async def _seeded(harness, **overrides):
    client, app = await harness(**overrides)
    await seed(app.state.session_factory, now=app.state.clock.now())
    return client, app


async def _cross(client, *, store_id: str = "nyc", quantity: int = 2, key: str) -> None:
    response = await put_inventory(
        client, sku=HERO, store_id=store_id, quantity=quantity, version=1, key=key
    )
    assert response.status_code == 200, response.text


async def _suppress(client, customer_id: str) -> None:
    response = await client.post(
        "/api/v1/email-suppressions",
        json={"customer_id": customer_id, "reason": "unsubscribed"},
    )
    assert response.status_code == 201, response.text


async def _handoffs(client, query: str = "") -> list[dict]:
    response = await client.get(f"/api/v1/email-handoffs{query}")
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _email_drafts(client) -> list[dict]:
    response = await client.get(f"/api/v1/email-drafts?sku={HERO}&store_id=nyc")
    return response.json()["items"]


async def test_worker_hands_off_new_york_recipients_and_leaves_draft_queued(harness) -> None:
    client, app = await _seeded(harness)
    await _cross(client, key="handoff-happy-0001")
    [draft] = await _email_drafts(client)
    before = _decisions("handed_off")

    result = await run_once(app.state.settings, clock=app.state.clock)

    assert result == HandoffBatchResult(
        examined=1, handed_off=1, suppressed=0, expired=0, skipped=0
    )
    assert _decisions("handed_off") == before + 1
    [handoff] = await _handoffs(client, f"?sku={HERO}&store_id=nyc")
    assert handoff["status"] == "handed_off"
    assert handoff["email_draft_id"] == draft["id"]
    assert handoff["suppressed_customer_ids"] == []
    payload = handoff["payload"]
    assert {row["customer_id"] for row in payload["recipients"]} == RECIPIENTS
    assert "cus_1009" not in {row["customer_id"] for row in payload["recipients"]}
    for field in COPIED_FIELDS:
        assert payload[field] == draft["payload"][field], field
    assert "did not send" in payload["handoff_note"]

    [after] = await _email_drafts(client)
    assert after["status"] == "queued"
    assert after["dedupe_key"] == draft["dedupe_key"]
    assert after["payload"] == draft["payload"]


async def test_suppressed_customer_is_removed_from_the_handoff(harness) -> None:
    client, app = await _seeded(harness)
    await _suppress(client, "cus_1842")
    await _cross(client, key="handoff-supp-00001")

    await app.state.container.handoffs.run_batch()

    [handoff] = await _handoffs(client)
    assert handoff["status"] == "handed_off"
    recipients = {row["customer_id"] for row in handoff["payload"]["recipients"]}
    assert recipients == RECIPIENTS - {"cus_1842"}
    assert handoff["suppressed_customer_ids"] == ["cus_1842"]
    [draft] = await _email_drafts(client)
    assert "cus_1842" in {row["customer_id"] for row in draft["payload"]["recipients"]}


async def test_every_recipient_suppressed_records_suppressed(harness) -> None:
    client, app = await _seeded(harness)
    for customer_id in sorted(RECIPIENTS):
        await _suppress(client, customer_id)
    await _cross(client, key="handoff-all-000001")
    before = _decisions("suppressed")

    await app.state.container.handoffs.run_batch()

    [handoff] = await _handoffs(client, "?status=suppressed")
    assert handoff["payload"]["recipients"] == []
    assert sorted(handoff["suppressed_customer_ids"]) == sorted(RECIPIENTS)
    assert handoff["payload"]["subject"] == "Only 2 left in Hudson Market — act now"
    assert handoff["payload"]["body"]
    assert "suppression list" in handoff["payload"]["handoff_note"]
    assert _decisions("suppressed") == before + 1


async def test_second_run_does_not_insert_or_count_again(harness) -> None:
    client, app = await _seeded(harness)
    await _cross(client, key="handoff-twice-0001")
    service = app.state.container.handoffs
    before = _decisions("handed_off")

    concurrent = await asyncio.gather(service.run_batch(), service.run_batch())
    again = await service.run_batch()

    assert sum(result.handed_off for result in concurrent) == 1
    assert again == HandoffBatchResult(examined=0, handed_off=0, suppressed=0, expired=0, skipped=0)
    assert len(await _handoffs(client)) == 1
    assert _decisions("handed_off") == before + 1


async def test_race_loser_skips_without_overwriting_the_first_decision(harness) -> None:
    client, app = await _seeded(harness)
    await _cross(client, key="handoff-race-00001")
    service = app.state.container.handoffs
    async with app.state.uow_factory() as uow:
        [draft] = await uow.handoffs.pending_drafts(limit=10)

    winner = await service.record(draft)
    assert winner is not None
    # The loser picked the same draft before the winner committed. A suppression
    # added since then would change its decision, so an overwrite would be visible.
    await _suppress(client, "cus_1842")
    counts = (_decisions("handed_off"), _decisions("suppressed"))

    loser = await service.record(draft)

    assert loser is None
    [stored] = await _handoffs(client)
    assert stored["id"] == winner.id
    assert stored["suppressed_customer_ids"] == []
    assert (_decisions("handed_off"), _decisions("suppressed")) == counts


async def test_draft_older_than_max_age_expires_without_recipients(harness) -> None:
    client, app = await _seeded(harness, email_handoff_max_age_hours=1)
    await _cross(client, key="handoff-old-000001")
    [draft] = await _email_drafts(client)
    app.state.clock.advance(timedelta(hours=1, seconds=1))
    before = _decisions("expired")

    result = await app.state.container.handoffs.run_batch()

    assert result.expired == 1
    [handoff] = await _handoffs(client, "?status=expired")
    assert handoff["suppressed_customer_ids"] == []
    payload = handoff["payload"]
    assert "recipients" not in payload
    assert payload["email_draft_id"] == draft["id"]
    assert payload["sku"] == HERO
    assert payload["store_id"] == "nyc"
    assert "aged out" in payload["handoff_note"]
    assert _decisions("expired") == before + 1


async def test_draft_exactly_at_max_age_is_still_handed_off(harness) -> None:
    client, app = await _seeded(harness)
    await _cross(client, key="handoff-edge-00001")
    app.state.clock.advance(timedelta(hours=72))

    result = await app.state.container.handoffs.run_batch()

    assert result.handed_off == 1
    assert result.expired == 0


async def test_sold_out_skip_produces_no_handoff(harness) -> None:
    client, app = await _seeded(harness)
    await _cross(client, quantity=0, key="handoff-sold-00001")
    assert await _email_drafts(client) == []

    result = await app.state.container.handoffs.run_batch()

    assert result.examined == 0
    assert await _handoffs(client) == []


async def test_oldest_first_batches_filters_and_empty_recipient_drafts(harness) -> None:
    client, app = await _seeded(harness, email_handoff_batch_size=1)
    await _cross(client, store_id="nyc", key="handoff-nyc-000001")
    app.state.clock.advance(timedelta(minutes=5))
    # Austin has no recent interactions for the pack, so its draft has nobody to mail.
    await _cross(client, store_id="aus", key="handoff-aus-000001")
    service = app.state.container.handoffs

    first = await service.run_batch()
    assert (first.examined, first.handed_off) == (1, 1)
    assert [item["status"] for item in await _handoffs(client, "?store_id=nyc")] == ["handed_off"]
    assert await _handoffs(client, "?store_id=aus") == []

    second = await service.run_batch()
    assert (second.examined, second.suppressed) == (1, 1)
    [austin] = await _handoffs(client, f"?sku={HERO}&store_id=aus")
    assert austin["status"] == "suppressed"
    assert austin["payload"]["recipients"] == []
    assert austin["suppressed_customer_ids"] == []
    assert "no recipients" in austin["payload"]["handoff_note"]

    assert len(await _handoffs(client, "?status=handed_off")) == 1
    assert await _handoffs(client, "?sku=SOCK-MER-3PK") == []
    assert len(await _handoffs(client, "?limit=1&offset=1")) == 1


async def test_suppression_api_rejects_duplicates_and_non_opaque_ids(harness) -> None:
    client, _app = await harness()
    created = await client.post(
        "/api/v1/email-suppressions",
        json={"customer_id": "cus_1842", "reason": "unsubscribed"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["customer_id"] == "cus_1842"
    assert created.json()["reason"] == "unsubscribed"

    duplicate = await client.post(
        "/api/v1/email-suppressions",
        json={"customer_id": "cus_1842", "reason": "again"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"

    for body in (
        {"customer_id": "someone@example.com", "reason": "unsubscribed"},
        {"customer_id": "x" * 65, "reason": "unsubscribed"},
        {"customer_id": "cus_2", "reason": "r" * 201},
        {"customer_id": "cus_2"},
    ):
        rejected = await client.post("/api/v1/email-suppressions", json=body)
        assert rejected.status_code == 422, body

    listed = await client.get("/api/v1/email-suppressions?limit=10&offset=0")
    assert listed.status_code == 200
    assert [item["customer_id"] for item in listed.json()["items"]] == ["cus_1842"]
    assert (listed.json()["limit"], listed.json()["offset"]) == (10, 0)


@pytest.mark.sqlite_only
def test_mailer_command_runs_once_on_a_fresh_database(tmp_path, monkeypatch, capsys) -> None:
    database = (tmp_path / "fresh.db").resolve().as_posix()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database}")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    assert Settings().database_url.endswith("fresh.db")

    main()

    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary == {
        "examined": 0,
        "handed_off": 0,
        "suppressed": 0,
        "expired": 0,
        "skipped": 0,
    }
