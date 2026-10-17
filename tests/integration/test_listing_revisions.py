"""Live listing history and rollback through the normal publish step."""

import json

from tests.fakes import FixedLLM
from tests.support import create_product, create_store

SKU = "BKPK-28L-OLV"
TEXT_FIELDS = ("title", "short_description", "long_description", "meta_title", "meta_description")
# Two different valid listings for the support product (color olive), so a
# rollback visibly brings back the older text.
FIRST = {
    "title": "Trailhead Pack",
    "short_description": "Color olive [[color]].",
    "long_description": "A day pack in olive [[color]].",
    "meta_title": "Trailhead Pack",
    "meta_description": "Olive day pack.",
    "cited_attributes": ["color"],
}
SECOND = {
    "title": "Trailhead Day Pack",
    "short_description": "An olive [[color]] day pack.",
    "long_description": "Built for the commute, in olive [[color]].",
    "meta_title": "Trailhead Day Pack",
    "meta_description": "Olive commuter day pack.",
    "cited_attributes": ["color"],
}
REVISIONS_URL = f"/api/v1/listings/{SKU}/stores/nyc/revisions"
ROLLBACK_URL = f"/api/v1/listings/{SKU}/stores/nyc/rollback"
LIVE_URL = f"/api/v1/listings/{SKU}/stores/nyc"


async def _ready_draft(client, llm: FixedLLM, copy: dict) -> dict:
    llm.set_content(json.dumps(copy))
    response = await client.post(f"/api/v1/listings/{SKU}/stores/nyc/rewrite")
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "ready", response.json()["violations"]
    return response.json()


async def _publish(client, draft_id: str, expected_version: int):
    return await client.post(
        f"/api/v1/listings/drafts/{draft_id}/publish",
        json={"expected_version": expected_version},
    )


async def _revisions(client) -> list[dict]:
    response = await client.get(REVISIONS_URL)
    assert response.status_code == 200, response.text
    return response.json()["items"]


async def _two_live_versions(harness):
    llm = FixedLLM(json.dumps(FIRST))
    client, app = await harness(llm=llm)
    await create_store(client)
    await create_product(client)
    first = await _ready_draft(client, llm, FIRST)
    assert (await _publish(client, first["id"], 0)).status_code == 200
    second = await _ready_draft(client, llm, SECOND)
    published = await _publish(client, second["id"], 1)
    assert published.status_code == 200, published.text
    assert published.json()["version"] == 2
    return client, app, first, second


async def test_publish_records_stripped_history_and_a_repeat_adds_nothing(harness) -> None:
    llm = FixedLLM(json.dumps(FIRST))
    client, _app = await harness(llm=llm)
    await create_store(client)
    await create_product(client)
    draft = await _ready_draft(client, llm, FIRST)

    published = await _publish(client, draft["id"], 0)

    assert published.status_code == 200, published.text
    live = published.json()
    [revision] = await _revisions(client)
    assert revision["version"] == 1
    assert revision["published_draft_id"] == draft["id"]
    for field in TEXT_FIELDS:
        assert "[[" not in revision[field], field
        assert revision[field] == live[field], field
    assert revision["published_at"] == live["published_at"]
    drafts = (await client.get(f"/api/v1/listings/drafts?sku={SKU}&store_id=nyc")).json()
    assert "[[color]]" in drafts["items"][0]["short_description"]
    product = (await client.get(f"/api/v1/products/{SKU}")).json()
    assert product["seo_title"] == "Live title"

    again = await _publish(client, draft["id"], 1)

    assert again.status_code == 200, again.text
    assert again.json()["version"] == 1
    assert len(await _revisions(client)) == 1


async def test_second_publish_adds_a_revision(harness) -> None:
    client, _app, first, second = await _two_live_versions(harness)

    revisions = await _revisions(client)

    assert [item["version"] for item in revisions] == [2, 1]
    assert [item["published_draft_id"] for item in revisions] == [second["id"], first["id"]]
    assert (await client.get(LIVE_URL)).json()["version"] == 2
    page = await client.get(f"{REVISIONS_URL}?limit=1&offset=1")
    assert [item["version"] for item in page.json()["items"]] == [1]


async def test_rollback_republishes_an_earlier_revision(harness) -> None:
    client, _app, first, _second = await _two_live_versions(harness)
    [_, original] = await _revisions(client)

    rolled = await client.post(ROLLBACK_URL, json={"revision_version": 1, "expected_version": 2})

    assert rolled.status_code == 200, rolled.text
    live = rolled.json()
    assert live["version"] == 3
    assert live["published_draft_id"] == first["id"]
    for field in TEXT_FIELDS:
        assert live[field] == original[field], field
        assert "[[" not in live[field], field
    revisions = await _revisions(client)
    assert [item["version"] for item in revisions] == [3, 2, 1]
    assert revisions[0]["published_draft_id"] == first["id"]
    product = (await client.get(f"/api/v1/products/{SKU}")).json()
    assert product["seo_title"] == "Live title"

    current = await client.post(ROLLBACK_URL, json={"revision_version": 3, "expected_version": 3})
    assert current.status_code == 409
    assert current.json()["error"] == {
        "code": "conflict",
        "message": "That revision is already live",
    }
    assert len(await _revisions(client)) == 3

    stale = await client.post(ROLLBACK_URL, json={"revision_version": 2, "expected_version": 2})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "optimistic_conflict"
    assert stale.json()["error"]["current_version"] == 3
    assert len(await _revisions(client)) == 3
    assert (await client.get(LIVE_URL)).json()["version"] == 3


async def test_rollback_refuses_a_draft_that_no_longer_matches_the_product(harness) -> None:
    client, _app, _first, _second = await _two_live_versions(harness)
    changed = await client.patch(
        f"/api/v1/products/{SKU}",
        json={
            "attributes": {
                "color": "black",
                "capacity_l": 28,
                "weight_g": 890,
                "material": "ripstop nylon",
            }
        },
    )
    assert changed.status_code == 200, changed.text

    refused = await client.post(ROLLBACK_URL, json={"revision_version": 1, "expected_version": 2})

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "conflict"
    assert (await client.get(LIVE_URL)).json()["version"] == 2
    assert len(await _revisions(client)) == 2


async def test_rollback_needs_a_live_listing_and_an_existing_revision(harness) -> None:
    llm = FixedLLM(json.dumps(FIRST))
    client, _app = await harness(llm=llm)
    await create_store(client)
    await create_product(client)

    no_live = await client.post(ROLLBACK_URL, json={"revision_version": 1, "expected_version": 0})
    assert no_live.status_code == 404
    assert await _revisions(client) == []

    draft = await _ready_draft(client, llm, FIRST)
    assert (await _publish(client, draft["id"], 0)).status_code == 200
    missing = await client.post(ROLLBACK_URL, json={"revision_version": 9, "expected_version": 1})
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert len(await _revisions(client)) == 1
