from __future__ import annotations

import asyncio
import importlib
import sqlite3

from fastapi.testclient import TestClient

from app.db import Database


VALID_THUMBNAIL = "https://i.ytimg.com/vi/poster-video/hqdefault.jpg"
GENUINE_AVATAR = "https://yt3.ggpht.com/channel/avatar=s88-c-k-c0x00ffffff-no-rj"


async def create_source(
    db: Database,
    suffix: str,
    *,
    kind: str = "channel",
    reference: str | None = None,
    safety_verdict: str = "SAFE",
    state: str = "approved",
    avatar_url: str = "",
) -> dict:
    source = await db.catalog_create(
        "source",
        {
            "kind": kind,
            "reference": reference or f"UC-poster-{suffix}",
            "title": f"Poster source {suffix}",
            "avatar_url": avatar_url,
            "correlation_id": f"poster-source-{suffix}",
        },
    )
    await db.catalog_source_safety_update(
        source["id"],
        verdict=safety_verdict,
        reason=f"poster source {suffix}",
        actor="test",
        correlation_id=f"poster-safety-{suffix}",
        policy_version="test-v1",
    )
    if state in {"approved", "blocked", "revoked"}:
        await db.catalog_transition(
            "source",
            source["id"],
            {
                "state": "approved",
                "actor": "test",
                "reason": f"poster source {suffix}",
                "correlation_id": f"poster-approved-{suffix}",
            },
        )
    if state != "approved":
        await db.catalog_transition(
            "source",
            source["id"],
            {
                "state": state,
                "actor": "test",
                "reason": f"poster source {suffix}",
                "correlation_id": f"poster-state-{suffix}",
            },
        )
    return await db.catalog_get("source", source["id"])


async def create_item(
    db: Database,
    source: dict,
    suffix: str,
    *,
    thumbnail_url: str = VALID_THUMBNAIL,
    state: str = "approved",
) -> dict:
    item = await db.catalog_create(
        "item",
        {
            "video_id": f"poster-video-{suffix}",
            "title": f"Poster item {suffix}",
            "source_id": source["id"],
            "channel_id": source["reference"],
            "channel_title": source["title"],
            "thumbnail_url": thumbnail_url,
            "correlation_id": f"poster-item-{suffix}",
        },
    )
    if state != "candidate":
        await db.catalog_transition(
            "item",
            item["id"],
            {
                "state": state,
                "actor": "test",
                "reason": f"poster item {suffix}",
                "correlation_id": f"poster-item-state-{suffix}",
            },
        )
    return await db.catalog_get("item", item["id"])


def load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    return importlib.reload(importlib.import_module("app.main"))


def test_parent_poster_listing_selection_reset_and_audit(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = Database(str(db_path))
    asyncio.run(db.init())
    source = asyncio.run(create_source(db, "valid"))
    valid_item = asyncio.run(create_item(db, source, "valid"))
    candidate_item = asyncio.run(
        create_item(db, source, "candidate", state="candidate")
    )
    invalid_thumbnail_item = asyncio.run(
        create_item(
            db,
            source,
            "invalid-thumbnail",
            thumbnail_url="https://example.test/video.jpg",
        )
    )
    other_source = asyncio.run(create_source(db, "other"))
    other_item = asyncio.run(create_item(db, other_source, "other"))
    before_revision = asyncio.run(db.catalog_revision())

    module = load_app(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        listing = client.get(f"/api/kids/sources/{source['id']}/poster-items")
        assert listing.status_code == 200
        assert listing.json() == {
            "source_id": source["id"],
            "items": [
                {
                    "id": valid_item["id"],
                    "title": valid_item["title"],
                    "thumbnail_url": VALID_THUMBNAIL,
                    "state": "approved",
                }
            ],
        }

        selected = client.put(
            f"/api/kids/sources/{source['id']}/poster",
            json={
                "item_id": valid_item["id"],
                "actor": "parent-test",
                "reason": "Use the approved poster",
                "correlation_id": "poster-select-1",
            },
        )
        assert selected.status_code == 200
        selected_body = selected.json()
        assert selected_body["source"]["id"] == source["id"]
        assert selected_body["source"]["poster_item_id"] == valid_item["id"]
        assert (
            selected_body["source"]["effective_poster_thumbnail_url"]
            == VALID_THUMBNAIL
        )
        assert selected_body["effective_poster"]["mode"] == "explicit"
        assert selected_body["effective_poster"]["item"]["id"] == valid_item["id"]
        assert "safety_evidence_json" not in selected_body
        assert "correlation_id" not in selected_body
        assert "candidate_json" not in selected_body
        assert asyncio.run(db.catalog_revision()) == before_revision + 1

        events = asyncio.run(db.kids_audit_events())
        poster_event = next(
            event for event in events if event["event"] == "source_poster_changed"
        )
        assert poster_event["actor"] == "parent-test"
        assert poster_event["reason"] == "Use the approved poster"
        assert poster_event["correlation_id"] == "poster-select-1"
        assert poster_event["revision"] == before_revision + 1

        no_op = client.put(
            f"/api/kids/sources/{source['id']}/poster",
            json={
                "item_id": valid_item["id"],
                "actor": "parent-test",
                "reason": "Same poster",
                "correlation_id": "poster-no-op",
            },
        )
        assert no_op.status_code == 200
        assert asyncio.run(db.catalog_revision()) == before_revision + 1
        assert len(
            [
                event
                for event in asyncio.run(db.kids_audit_events())
                if event["event"] == "source_poster_changed"
            ]
        ) == 1

        reset = client.put(
            f"/api/kids/sources/{source['id']}/poster",
            json={
                "item_id": None,
                "actor": "parent-test",
                "reason": "Return to automatic poster",
                "correlation_id": "poster-reset-1",
            },
        )
        assert reset.status_code == 200
        reset_body = reset.json()
        assert reset_body["source"]["poster_item_id"] is None
        assert reset_body["effective_poster"]["mode"] == "automatic"
        assert reset_body["effective_poster"]["item"]["id"] == valid_item["id"]
        assert asyncio.run(db.catalog_revision()) == before_revision + 2
        reset_event = next(
            event
            for event in asyncio.run(db.kids_audit_events())
            if event["correlation_id"] == "poster-reset-1"
        )
        assert reset_event["event"] == "source_poster_changed"

    assert candidate_item["id"] != valid_item["id"]
    assert invalid_thumbnail_item["id"] != valid_item["id"]
    assert other_item["id"] != valid_item["id"]


def test_parent_poster_rejects_cross_source_unapproved_and_non_proxyable(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "sentinel.db"
    db = Database(str(db_path))
    asyncio.run(db.init())
    source = asyncio.run(create_source(db, "validation"))
    candidate = asyncio.run(
        create_item(db, source, "candidate", state="candidate")
    )
    non_proxyable = asyncio.run(
        create_item(
            db,
            source,
            "non-proxyable",
            thumbnail_url="https://example.test/poster.jpg",
        )
    )
    other_source = asyncio.run(create_source(db, "foreign"))
    foreign_item = asyncio.run(create_item(db, other_source, "foreign"))
    before_revision = asyncio.run(db.catalog_revision())

    module = load_app(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        for item_id in (candidate["id"], non_proxyable["id"], foreign_item["id"]):
            response = client.put(
                f"/api/kids/sources/{source['id']}/poster",
                json={
                    "item_id": item_id,
                    "actor": "parent-test",
                    "reason": "Invalid poster",
                    "correlation_id": f"poster-invalid-{item_id}",
                },
            )
            assert response.status_code == 409

    assert asyncio.run(db.catalog_revision()) == before_revision
    assert not any(
        event["event"] == "source_poster_changed"
        for event in asyncio.run(db.kids_audit_events())
    )


def test_parent_poster_rejects_mismatched_channel_identity_in_listing_and_selection(
    tmp_path, monkeypatch
):
    db = Database(str(tmp_path / "sentinel.db"))
    asyncio.run(db.init())
    source = asyncio.run(create_source(db, "identity"))
    valid_item = asyncio.run(create_item(db, source, "valid"))
    mismatched_item = asyncio.run(create_item(db, source, "mismatched"))
    with sqlite3.connect(db.db_path) as connection:
        connection.execute(
            "UPDATE catalog_items SET channel_id=? WHERE id=?",
            ("UC-a-different-channel", mismatched_item["id"]),
        )
        connection.commit()

    module = load_app(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        listing = client.get(f"/api/kids/sources/{source['id']}/poster-items")
        assert listing.status_code == 200
        assert [item["id"] for item in listing.json()["items"]] == [valid_item["id"]]

        selection = client.put(
            f"/api/kids/sources/{source['id']}/poster",
            json={
                "item_id": mismatched_item["id"],
                "actor": "parent-test",
                "reason": "Reject mismatched channel identity",
                "correlation_id": "poster-mismatched-channel",
            },
        )
        assert selection.status_code == 409


def test_parent_poster_rejects_ineligible_source(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = Database(str(db_path))
    asyncio.run(db.init())
    unsafe_source = asyncio.run(
        create_source(db, "unsafe", safety_verdict="UNSAFE")
    )
    unsafe_item = asyncio.run(create_item(db, unsafe_source, "unsafe"))
    revoked_source = asyncio.run(create_source(db, "revoked", state="revoked"))
    revoked_item = asyncio.run(create_item(db, revoked_source, "revoked"))
    before_revision = asyncio.run(db.catalog_revision())

    module = load_app(tmp_path, monkeypatch)
    with TestClient(module.app) as client:
        unsafe = client.put(
            f"/api/kids/sources/{unsafe_source['id']}/poster",
            json={
                "item_id": unsafe_item["id"],
                "correlation_id": "poster-unsafe",
            },
        )
        revoked = client.put(
            f"/api/kids/sources/{revoked_source['id']}/poster",
            json={
                "item_id": revoked_item["id"],
                "correlation_id": "poster-revoked",
            },
        )
        assert unsafe.status_code == 409
        assert revoked.status_code == 409

    assert asyncio.run(db.catalog_revision()) == before_revision


def test_parent_source_listing_blanks_untrusted_genuine_avatar(tmp_path, monkeypatch):
    db = Database(str(tmp_path / "sentinel.db"))
    asyncio.run(db.init())
    source = asyncio.run(
        create_source(
            db,
            "untrusted-avatar",
            avatar_url="https://evil.example/channel-avatar.jpg",
        )
    )
    asyncio.run(create_item(db, source, "untrusted-avatar"))
    module = load_app(tmp_path, monkeypatch)

    with TestClient(module.app) as client:
        response = client.get("/api/kids/sources")
        assert response.status_code == 200
        listed = next(
            row for row in response.json()["sources"] if row["id"] == source["id"]
        )
        assert listed["genuine_avatar_url"] == ""
        assert "evil.example" not in response.text


def test_sources_listing_separates_genuine_avatar_from_video_fallback(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    asyncio.run(db.init())
    fallback_source = asyncio.run(create_source(db, "fallback"))
    fallback_item = asyncio.run(create_item(db, fallback_source, "fallback"))
    genuine_source = asyncio.run(
        create_source(db, "genuine", avatar_url=GENUINE_AVATAR)
    )
    asyncio.run(create_item(db, genuine_source, "genuine"))

    rows = asyncio.run(db.catalog_sources_list(sort="id-asc"))
    fallback = next(row for row in rows if row["id"] == fallback_source["id"])
    genuine = next(row for row in rows if row["id"] == genuine_source["id"])

    assert fallback["avatar_url"] == VALID_THUMBNAIL
    assert fallback["fallback_avatar_url"] == VALID_THUMBNAIL
    assert fallback["genuine_avatar_url"] == ""
    assert fallback["poster_item_id"] is None
    assert fallback["effective_poster_thumbnail_url"] == VALID_THUMBNAIL
    assert genuine["avatar_url"] == GENUINE_AVATAR
    assert genuine["genuine_avatar_url"] == GENUINE_AVATAR
    assert genuine["fallback_avatar_url"] == VALID_THUMBNAIL
    assert fallback_item["id"] != genuine["id"]
