from tests.fakes import FixedLLM
from tests.support import create_product, create_store


def _invalid() -> str:
    return (
        '{"title":"'
        + ("T" * 80)
        + '","short_description":"x [[nope]]","long_description":"x [[nope]]",'
        + '"meta_title":"m","meta_description":"d","cited_attributes":["nope"]}'
    )


async def test_publish_copies_ready_draft_and_leaves_product_seo(harness) -> None:
    client, _app = await harness()
    await create_store(client)
    await create_product(client)
    created = await client.post("/api/v1/listings/BKPK-28L-OLV/stores/nyc/rewrite")
    assert created.status_code == 201, created.text
    draft_id = created.json()["id"]

    published = await client.post(
        f"/api/v1/listings/drafts/{draft_id}/publish",
        json={"expected_version": 0},
    )
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["version"] == 1
    assert body["published_draft_id"] == draft_id
    assert body["title"] == created.json()["title"]
    for field in (
        "title",
        "short_description",
        "long_description",
        "meta_title",
        "meta_description",
    ):
        assert "[[" not in body[field]

    live = await client.get("/api/v1/listings/BKPK-28L-OLV/stores/nyc")
    assert live.json() == body
    product = (await client.get("/api/v1/products/BKPK-28L-OLV")).json()
    assert product["seo_title"] == "Live title"

    stale_same = await client.post(
        f"/api/v1/listings/drafts/{draft_id}/publish",
        json={"expected_version": 0},
    )
    assert stale_same.status_code == 409
    assert stale_same.json()["error"]["current_version"] == 1
    again = await client.post(
        f"/api/v1/listings/drafts/{draft_id}/publish",
        json={"expected_version": 1},
    )
    assert again.status_code == 200
    assert again.json()["version"] == 1

    second = await client.post("/api/v1/listings/BKPK-28L-OLV/stores/nyc/rewrite")
    assert second.status_code == 201
    stale = await client.post(
        f"/api/v1/listings/drafts/{second.json()['id']}/publish",
        json={"expected_version": 0},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["current_version"] == 1
    replaced = await client.post(
        f"/api/v1/listings/drafts/{second.json()['id']}/publish",
        json={"expected_version": 1},
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["version"] == 2
    assert replaced.json()["published_draft_id"] == second.json()["id"]
    assert (await client.get("/api/v1/products/BKPK-28L-OLV")).json()["seo_title"] == "Live title"


async def test_publish_refuses_a_draft_after_the_catalog_changes(harness) -> None:
    client, _app = await harness()
    await create_store(client)
    await create_product(client)
    created = await client.post("/api/v1/listings/BKPK-28L-OLV/stores/nyc/rewrite")
    assert created.status_code == 201, created.text
    patched = await client.patch(
        "/api/v1/products/BKPK-28L-OLV",
        json={
            "attributes": {
                "color": "black",
                "capacity_l": 30,
                "weight_g": 890,
                "material": "recycled polyester",
            }
        },
    )
    assert patched.status_code == 200, patched.text
    denied = await client.post(
        f"/api/v1/listings/drafts/{created.json()['id']}/publish",
        json={"expected_version": 0},
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "conflict"
    missing = await client.get("/api/v1/listings/BKPK-28L-OLV/stores/nyc")
    assert missing.status_code == 404


async def test_validation_failed_draft_cannot_be_published(harness) -> None:
    client, _app = await harness(llm=FixedLLM(_invalid()))
    await create_store(client)
    await create_product(client)
    created = await client.post("/api/v1/listings/BKPK-28L-OLV/stores/nyc/rewrite")
    assert created.status_code == 201
    assert created.json()["status"] == "validation_failed"
    denied = await client.post(
        f"/api/v1/listings/drafts/{created.json()['id']}/publish",
        json={"expected_version": 0},
    )
    assert denied.status_code == 409
    assert denied.json()["error"]["code"] == "conflict"
    missing = await client.get("/api/v1/listings/BKPK-28L-OLV/stores/nyc")
    assert missing.status_code == 404
