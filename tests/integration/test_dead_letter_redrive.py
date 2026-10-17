"""Dead-letter redrive: one named handler, recorded attempts, replay after success."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from tests.fakes import FixedLLM
from tests.support import create_product, create_store, put_inventory

from catalogguard.domain.events import InventoryChanged
from catalogguard.domain.models import DeadLetter
from catalogguard.infrastructure.db.orm import DeadLetterRedriveClaimORM, OutboxORM
from catalogguard.infrastructure.db.repositories import DeadLetterRedriveClaimRepository
from catalogguard.infrastructure.events.serialize import dump_event
from catalogguard.infrastructure.metrics import REGISTRY

SKU = "BKPK-28L-OLV"
LOW_STOCK_NAMESPACE = uuid.UUID("6f1d8c2a-4b7e-5d19-8c33-1a2b3c4d5e6f")
IN_PROGRESS = {
    "code": "conflict",
    "message": "A redrive of this dead letter is already in progress",
}
CLAIM_TIMEOUT = timedelta(seconds=300)


def _valid_listing() -> str:
    # Same shape as tests/unit/test_listing_validation.py _valid(); the support
    # product is olive, so this passes the citation window rule.
    return json.dumps(
        {
            "title": "Trailhead Pack",
            "short_description": "Color olive [[color]].",
            "long_description": "A day pack in olive [[color]].",
            "meta_title": "Trailhead Pack",
            "meta_description": "Olive day pack.",
            "cited_attributes": ["color"],
        }
    )


def _metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _redrives(result: str) -> float:
    return _metric("catalogguard_dead_letter_redrives_total", result=result)


async def _letters(client) -> list[dict]:
    response = await client.get("/api/v1/dead-letters")
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _listing_drafts(client) -> list[dict]:
    response = await client.get(f"/api/v1/listings/drafts?sku={SKU}&store_id=nyc")
    return response.json()["items"]


async def _email_drafts(client) -> list[dict]:
    response = await client.get(f"/api/v1/email-drafts?sku={SKU}&store_id=nyc")
    return response.json()["items"]


async def _redrive(client, letter_id: str):
    return await client.post(f"/api/v1/dead-letters/{letter_id}/redrive")


async def _cross(client, prefix: str) -> None:
    created = await put_inventory(client, quantity=10, version=0, key=f"{prefix}-create-01")
    assert created.status_code == 201, created.text
    dropped = await put_inventory(client, quantity=2, version=1, key=f"{prefix}-drop-0001")
    assert dropped.status_code == 200, dropped.text


async def _malformed_listing_letter(harness):
    llm = FixedLLM("this is not json")
    client, app = await harness(llm=llm)
    await create_store(client)
    await create_product(client)
    await _cross(client, "redrive-llm")
    [letter] = await _letters(client)
    assert letter["handler"] == "listing_rewrite"
    assert await _listing_drafts(client) == []
    assert len(await _email_drafts(client)) == 1
    return client, app, llm, letter


async def _store_letter(app, *, handler: str, event_type: str, payload: dict) -> str:
    letter = DeadLetter(
        id=str(uuid.uuid4()),
        event_id=str(payload.get("event_id", uuid.uuid4())),
        event_type=event_type,
        handler=handler,
        payload=payload,
        error="RuntimeError: original failure",
        attempts=2,
        created_at=app.state.clock.now(),
    )
    async with app.state.uow_factory() as uow:
        await uow.dead_letters.add(letter)
        await uow.commit()
    return letter.id


def _inventory_changed(app) -> InventoryChanged:
    return InventoryChanged(
        event_id=str(uuid.uuid4()),
        event_type="InventoryChanged",
        occurred_at=app.state.clock.now(),
        correlation_id="redrive-threshold",
        sku=SKU,
        store_id="nyc",
        previous_quantity=10,
        new_quantity=2,
        inventory_version=2,
    )


async def test_listing_redrive_recovers_malformed_output_once(harness) -> None:
    client, app, llm, letter = await _malformed_listing_letter(harness)
    assert (letter["redrive_count"], letter["last_redrive_status"]) == (0, None)
    assert letter["last_redriven_at"] is None
    llm.set_content(_valid_listing())
    calls = llm.calls
    dead_letters = _metric("catalogguard_dead_letters_total", handler="listing_rewrite")
    succeeded = _redrives("succeeded")

    response = await _redrive(client, letter["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dead_letter_id"] == letter["id"]
    assert body["handler"] == "listing_rewrite"
    assert (body["result"], body["replayed"]) == ("succeeded", False)
    assert llm.calls == calls + 1
    [draft] = await _listing_drafts(client)
    assert draft["status"] == "ready"
    assert draft["trigger_event_id"] == letter["event_id"]
    [email] = await _email_drafts(client)
    assert email["status"] == "queued"
    [kept] = await _letters(client)
    assert kept["id"] == letter["id"]
    assert (kept["redrive_count"], kept["last_redrive_status"]) == (1, "succeeded")
    assert kept["last_redriven_at"] == app.state.clock.now().isoformat()
    assert _redrives("succeeded") == succeeded + 1

    handler = app.state.bus.handler_for("LowInventoryDetected", "listing_rewrite")
    handled: list[object] = []

    async def spy(event) -> None:
        handled.append(event)

    handler.handle = spy
    again = await _redrive(client, letter["id"])
    del handler.handle

    assert again.status_code == 200, again.text
    assert again.json()["result"] == "replayed"
    assert again.json()["replayed"] is True
    assert again.json()["redrive_id"] == body["redrive_id"]
    assert handled == []
    assert llm.calls == calls + 1
    assert len(await _listing_drafts(client)) == 1
    assert (await _letters(client))[0]["redrive_count"] == 1
    assert _metric("catalogguard_dead_letters_total", handler="listing_rewrite") == dead_letters


async def test_concurrent_redrives_run_the_handler_once(harness) -> None:
    client, _app, llm, letter = await _malformed_listing_letter(harness)
    llm.set_content(_valid_listing())
    calls = llm.calls

    first, second = await asyncio.gather(
        _redrive(client, letter["id"]), _redrive(client, letter["id"])
    )

    assert {first.json()["result"], second.json()["result"]} == {"succeeded", "replayed"}
    assert llm.calls == calls + 1
    assert len(await _listing_drafts(client)) == 1


async def test_listing_redrive_respects_the_daily_cap_until_the_next_utc_day(harness) -> None:
    client, app = await harness(llm_daily_cap_per_sku=1)
    await create_store(client)
    await create_product(client)
    manual = await client.post(f"/api/v1/listings/{SKU}/stores/nyc/rewrite")
    assert manual.status_code == 201, manual.text
    await _cross(client, "redrive-cap")
    [letter] = await _letters(client)
    assert letter["handler"] == "listing_rewrite"
    dead_letters = _metric("catalogguard_dead_letters_total", handler="listing_rewrite")
    failed = _redrives("failed")

    capped = await _redrive(client, letter["id"])

    assert capped.status_code == 429
    assert capped.json()["error"]["code"] == "llm_cost_limit"
    [kept] = await _letters(client)
    assert (kept["redrive_count"], kept["last_redrive_status"]) == (1, "failed")
    drafts = await _listing_drafts(client)
    assert [item["reason"] for item in drafts] == ["manual"]
    assert _redrives("failed") == failed + 1

    app.state.clock.advance(timedelta(days=1))
    recovered = await _redrive(client, letter["id"])

    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["result"] == "succeeded"
    triggers = {item["trigger_event_id"] for item in await _listing_drafts(client)}
    assert letter["event_id"] in triggers
    letters = await _letters(client)
    assert len(letters) == 1
    assert (letters[0]["redrive_count"], letters[0]["last_redrive_status"]) == (2, "succeeded")
    assert _metric("catalogguard_dead_letters_total", handler="listing_rewrite") == dead_letters


async def test_outbox_decode_letter_is_refused_and_not_requeued(harness) -> None:
    client, app = await harness()
    await create_store(client)
    await create_product(client)
    async with app.state.session_factory() as session:
        session.add(
            OutboxORM(
                event_id="bad-row",
                event_type="InventoryChanged",
                payload={"event_type": "InventoryChanged"},
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
                published_at=None,
                claimed_at=None,
                claim_token=None,
            )
        )
        await session.commit()
    created = await put_inventory(client, quantity=10, version=0, key="redrive-poison-01")
    assert created.status_code == 201, created.text
    [letter] = await _letters(client)
    assert letter["handler"] == "outbox_decode"

    async def outbox_rows() -> int:
        async with app.state.session_factory() as session:
            return await session.scalar(select(func.count()).select_from(OutboxORM))

    rows = await outbox_rows()
    rejected = _redrives("rejected")

    response = await _redrive(client, letter["id"])

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "conflict",
        "message": "Undecodable outbox rows cannot be redriven",
    }
    [kept] = await _letters(client)
    assert kept["id"] == letter["id"]
    assert kept["redrive_count"] == 0
    assert await outbox_rows() == rows
    assert _redrives("rejected") == rejected + 1


async def test_unknown_dead_letter_is_404(harness) -> None:
    client, _app = await harness()
    response = await _redrive(client, "no-such-letter")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_undecodable_payload_and_unknown_handler_are_refused(harness) -> None:
    client, app = await harness()
    broken = await _store_letter(
        app,
        handler="listing_rewrite",
        event_type="LowInventoryDetected",
        payload={"event_type": "LowInventoryDetected"},
    )
    unknown = await _store_letter(
        app,
        handler="nope",
        event_type="InventoryChanged",
        payload=dump_event(_inventory_changed(app)),
    )
    rejected = _redrives("rejected")

    decode = await _redrive(client, broken)
    missing = await _redrive(client, unknown)

    assert decode.status_code == 409
    assert decode.json()["error"]["message"] == "Dead-letter payload cannot be decoded"
    assert missing.status_code == 409
    assert missing.json()["error"]["message"] == (
        "No handler named nope is registered for InventoryChanged"
    )
    assert all(item["redrive_count"] == 0 for item in await _letters(client))
    assert _redrives("rejected") == rejected + 2


async def test_email_redrive_queues_one_draft_and_replays(harness) -> None:
    client, app = await harness()
    await create_store(client)
    await create_product(client)
    handler = app.state.bus.handler_for("LowInventoryDetected", "email_draft")

    async def broken(event) -> None:
        raise RuntimeError("email draft store offline")

    handler.handle = broken
    await _cross(client, "redrive-email")
    del handler.handle
    [letter] = await _letters(client)
    assert letter["handler"] == "email_draft"
    assert await _email_drafts(client) == []
    assert len(await _listing_drafts(client)) == 1

    response = await _redrive(client, letter["id"])

    assert response.status_code == 200, response.text
    assert response.json()["result"] == "succeeded"
    [email] = await _email_drafts(client)
    assert email["status"] == "queued"
    assert email["trigger_event_id"] == letter["event_id"]
    assert len(await _listing_drafts(client)) == 1

    calls: list[object] = []
    original = handler.handle

    async def spy(event) -> None:
        calls.append(event)
        await original(event)

    handler.handle = spy
    again = await _redrive(client, letter["id"])
    del handler.handle

    assert again.json()["replayed"] is True
    assert calls == []
    assert len(await _email_drafts(client)) == 1


async def test_threshold_redrive_drains_listing_and_email(harness) -> None:
    client, app = await harness()
    await create_store(client)
    await create_product(client)
    event = _inventory_changed(app)
    letter_id = await _store_letter(
        app,
        handler="threshold_evaluation",
        event_type="InventoryChanged",
        payload=dump_event(event),
    )

    response = await _redrive(client, letter_id)

    assert response.status_code == 200, response.text
    assert response.json()["result"] == "succeeded"
    [draft] = await _listing_drafts(client)
    assert draft["trigger_event_id"] == str(uuid.uuid5(LOW_STOCK_NAMESPACE, event.event_id))
    [email] = await _email_drafts(client)
    assert email["status"] == "queued"
    assert (await client.get("/api/v1/email-handoffs")).json()["items"] == []

    again = await _redrive(client, letter_id)

    assert again.json()["replayed"] is True
    assert len(await _listing_drafts(client)) == 1
    assert len(await _email_drafts(client)) == 1
    assert (await client.get("/api/v1/email-handoffs")).json()["items"] == []


async def test_drain_failure_after_redrive_still_returns_success(harness) -> None:
    client, app = await harness()
    await create_store(client)
    await create_product(client)
    letter_id = await _store_letter(
        app,
        handler="threshold_evaluation",
        event_type="InventoryChanged",
        payload=dump_event(_inventory_changed(app)),
    )

    async def failing_drain() -> None:
        raise RuntimeError("outbox unavailable")

    app.state.bus._drain = failing_drain
    drain_failures = _metric("catalogguard_drain_failures_total")

    response = await _redrive(client, letter_id)

    assert response.status_code == 200, response.text
    assert response.json()["result"] == "succeeded"
    assert _metric("catalogguard_drain_failures_total") == drain_failures + 1
    letters = await _letters(client)
    assert [item["id"] for item in letters] == [letter_id]
    assert letters[0]["last_redrive_status"] == "succeeded"


async def test_failed_attempt_is_recorded_truncated_and_does_not_block_a_retry(harness) -> None:
    client, app = await harness()
    await create_store(client)
    await create_product(client)
    handler = app.state.bus.handler_for("LowInventoryDetected", "email_draft")

    async def broken(event) -> None:
        raise RuntimeError("x" * 3000)

    handler.handle = broken
    await _cross(client, "redrive-retry")
    [letter] = await _letters(client)

    with pytest.raises(RuntimeError):
        await _redrive(client, letter["id"])
    del handler.handle

    async with app.state.uow_factory() as uow:
        [attempt] = await uow.redrives.for_letters([letter["id"]])
    assert attempt.status == "failed"
    assert attempt.error == "x" * 2000
    assert (await _letters(client))[0]["last_redrive_status"] == "failed"

    # "Last" is the newest created_at; move the clock so the retry is newer.
    app.state.clock.advance(timedelta(seconds=1))
    retried = await _redrive(client, letter["id"])

    assert retried.status_code == 200, retried.text
    assert retried.json()["result"] == "succeeded"
    assert len(await _email_drafts(client)) == 1
    [kept] = await _letters(client)
    assert (kept["redrive_count"], kept["last_redrive_status"]) == (2, "succeeded")


async def _threshold_letter(harness):
    """A stored threshold letter, with a spy that counts calls to its handler."""

    client, app = await harness()
    await create_store(client)
    await create_product(client)
    letter_id = await _store_letter(
        app,
        handler="threshold_evaluation",
        event_type="InventoryChanged",
        payload=dump_event(_inventory_changed(app)),
    )
    handler = app.state.bus.handler_for("InventoryChanged", "threshold_evaluation")
    original = handler.handle
    handled: list[str] = []

    async def spy(event) -> None:
        handled.append(event.event_id)
        await original(event)

    handler.handle = spy
    return client, app, letter_id, handled


async def _claimed_at(app, letter_id: str) -> datetime | None:
    async with app.state.uow_factory() as uow:
        return await uow.redrive_claims.claimed_at(letter_id)


async def _attempt_statuses(app, letter_id: str) -> list[str]:
    async with app.state.uow_factory() as uow:
        return [item.status for item in await uow.redrives.for_letters([letter_id])]


async def _insert_claim(app, letter_id: str, claimed_at: datetime) -> None:
    async with app.state.session_factory() as session:
        session.add(DeadLetterRedriveClaimORM(dead_letter_id=letter_id, claimed_at=claimed_at))
        await session.commit()


async def test_a_fresh_claim_is_409_until_it_reaches_the_timeout(harness) -> None:
    client, app, letter_id, handled = await _threshold_letter(harness)
    clock = app.state.clock
    await _insert_claim(app, letter_id, clock.now())
    in_progress = _redrives("in_progress")

    busy = await _redrive(client, letter_id)

    assert busy.status_code == 409
    assert busy.json()["error"] == IN_PROGRESS
    assert handled == []
    assert await _attempt_statuses(app, letter_id) == []
    assert _redrives("in_progress") == in_progress + 1

    clock.advance(CLAIM_TIMEOUT - timedelta(seconds=1))
    assert (await _redrive(client, letter_id)).json()["error"] == IN_PROGRESS
    assert handled == []

    clock.advance(timedelta(seconds=1))
    taken = await _redrive(client, letter_id)

    assert taken.status_code == 200, taken.text
    assert taken.json()["result"] == "succeeded"
    assert len(handled) == 1
    assert await _claimed_at(app, letter_id) is None
    assert await _attempt_statuses(app, letter_id) == ["succeeded"]
    assert len(await _listing_drafts(client)) == 1

    replayed = await _redrive(client, letter_id)

    assert replayed.json()["result"] == "replayed"
    assert len(handled) == 1
    assert _redrives("in_progress") == in_progress + 2


async def test_a_cost_limited_redrive_releases_its_claim(harness) -> None:
    client, app = await harness(llm_daily_cap_per_sku=1)
    await create_store(client)
    await create_product(client)
    manual = await client.post(f"/api/v1/listings/{SKU}/stores/nyc/rewrite")
    assert manual.status_code == 201, manual.text
    await _cross(client, "redrive-claim-cap")
    [letter] = await _letters(client)
    assert letter["handler"] == "listing_rewrite"
    dead_letters = _metric("catalogguard_dead_letters_total", handler="listing_rewrite")
    handler = app.state.bus.handler_for("LowInventoryDetected", "listing_rewrite")
    original = handler.handle
    handled: list[str] = []

    async def spy(event) -> None:
        handled.append(event.event_id)
        await original(event)

    handler.handle = spy

    capped = await _redrive(client, letter["id"])

    assert capped.status_code == 429
    assert capped.json()["error"]["code"] == "llm_cost_limit"
    assert await _attempt_statuses(app, letter["id"]) == ["failed"]
    assert await _claimed_at(app, letter["id"]) is None

    app.state.clock.advance(timedelta(seconds=1))
    again = await _redrive(client, letter["id"])

    assert again.status_code == 429
    assert len(handled) == 2
    assert await _attempt_statuses(app, letter["id"]) == ["failed", "failed"]
    assert await _claimed_at(app, letter["id"]) is None

    app.state.clock.advance(timedelta(days=1))
    recovered = await _redrive(client, letter["id"])

    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["result"] == "succeeded"
    assert await _claimed_at(app, letter["id"]) is None
    assert len(await _letters(client)) == 1
    assert _metric("catalogguard_dead_letters_total", handler="listing_rewrite") == dead_letters


async def test_a_lost_takeover_race_is_409_and_does_not_run_the_handler(
    harness, monkeypatch
) -> None:
    client, app, letter_id, handled = await _threshold_letter(harness)
    clock = app.state.clock
    await _insert_claim(app, letter_id, clock.now())
    expired = clock.now() - CLAIM_TIMEOUT

    async def read_before_the_other_process_moved_it(self, dead_letter_id: str):
        return expired

    # This process read an expired claim, but another one moved claimed_at first.
    monkeypatch.setattr(
        DeadLetterRedriveClaimRepository, "claimed_at", read_before_the_other_process_moved_it
    )

    lost = await _redrive(client, letter_id)

    assert lost.status_code == 409
    assert lost.json()["error"] == IN_PROGRESS
    assert handled == []
    assert await _attempt_statuses(app, letter_id) == []
    monkeypatch.undo()
    assert await _claimed_at(app, letter_id) == clock.now()


async def test_an_overrun_attempt_does_not_release_the_claim_that_replaced_it(harness) -> None:
    client, app, letter_id, handled = await _threshold_letter(harness)
    clock = app.state.clock
    started = clock.now()
    handler = app.state.bus.handler_for("InventoryChanged", "threshold_evaluation")
    spy = handler.handle

    async def overrun(event) -> None:
        # The handler outlives the timeout, and a second process takes the claim.
        clock.advance(CLAIM_TIMEOUT)
        async with app.state.uow_factory() as uow:
            assert await uow.redrive_claims.take_over(letter_id, expected=started, now=clock.now())
            await uow.commit()
        await spy(event)

    handler.handle = overrun

    finished = await _redrive(client, letter_id)

    assert finished.status_code == 200, finished.text
    assert finished.json()["result"] == "succeeded"
    assert await _claimed_at(app, letter_id) == started + CLAIM_TIMEOUT
    async with app.state.uow_factory() as uow:
        assert not await uow.redrive_claims.take_over(letter_id, expected=started, now=clock.now())
    replayed = await _redrive(client, letter_id)
    assert replayed.json()["result"] == "replayed"
    assert len(handled) == 1


@pytest.mark.parametrize("timeout_seconds", [0, -1])
async def test_claim_timeout_must_be_positive(harness, timeout_seconds: int) -> None:
    with pytest.raises(RuntimeError, match="REDRIVE_CLAIM_TIMEOUT_SECONDS"):
        await harness(redrive_claim_timeout_seconds=timeout_seconds)
