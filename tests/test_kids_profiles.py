from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from tests.test_kids_dataplane import seed_catalog


def test_default_profiles_and_assignment_changes_survive_reinit(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    asyncio.run(db.init())

    profiles = asyncio.run(db.kids_profiles_list())
    assert [(row["slug"], row["age_years"]) for row in profiles] == [
        ("noah", 6),
        ("felix", 2),
    ]
    assert [row["avatar_key"] for row in profiles] == ["hare.fill", "tortoise.fill"]

    source = asyncio.run(
        db.catalog_create(
            "source",
            {
                "kind": "channel",
                "reference": "UC-profile-test",
                "title": "Profile test",
                "correlation_id": "profile-source",
            },
        )
    )
    assert asyncio.run(db.kids_source_profile_slugs(source["id"])) == ["noah"]
    asyncio.run(
        db.catalog_source_safety_update(
            source["id"],
            verdict="SAFE",
            language="nl",
            content_kind="learning",
            age_suitability={"2": "SUITABLE", "6": "SUITABLE"},
            reason="profile test",
            actor="test",
            correlation_id="profile-safety",
        )
    )

    asyncio.run(
        db.kids_source_profiles_set(
            source["id"],
            ["felix"],
            actor="test",
            reason="Felix-only source",
            correlation_id="profile-assignment",
        )
    )
    asyncio.run(db.init())
    assert asyncio.run(db.kids_source_profile_slugs(source["id"])) == ["felix"]

    asyncio.run(
        db.catalog_source_safety_update(
            source["id"],
            verdict="SAFE",
            language="en",
            content_kind="learning",
            age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
            reason="English source for older child",
            actor="test",
            correlation_id="profile-safety-change",
        )
    )
    with pytest.raises(ValueError, match="not suitable"):
        asyncio.run(
            db.kids_source_profiles_set(
                source["id"],
                ["felix"],
                actor="test",
                reason="Invalid Felix assignment",
                correlation_id="invalid-profile-assignment",
            )
        )


def test_feed_is_separated_by_profile(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(1080, 1080)))
    source = asyncio.run(db.catalog_sources_list())[0]
    asyncio.run(
        db.kids_source_profiles_set(
            source["id"],
            ["felix"],
            actor="test",
            reason="Felix-only feed test",
            correlation_id="profile-feed",
        )
    )

    monkeypatch.setenv("SENTINEL_DB_PATH", str(db_path))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        noah = client.get("/v1/kids/feed", params={"profile": "noah", "limit": 10})
        felix = client.get("/v1/kids/feed", params={"profile": "felix", "limit": 1})
        assert noah.status_code == 200
        assert felix.status_code == 200
        assert noah.json()["items"] == []
        assert len(felix.json()["items"]) == 1
        assert felix.json()["next_cursor"]

        next_page = client.get(
            "/v1/kids/feed",
            params={"cursor": felix.json()["next_cursor"], "limit": 1},
        )
        assert next_page.status_code == 200
        assert len(next_page.json()["items"]) == 1


def test_source_api_exposes_profiles_and_assignment_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        source = client.post(
            "/api/kids/sources",
            json={
                "kind": "channel",
                "reference": "UC-profile-api",
                "title": "Profile API",
            },
        )
        assert source.status_code == 200
        source_id = source.json()["id"]
        asyncio.run(
            module.app.state.runtime.db.catalog_source_safety_update(
                source_id,
                verdict="SAFE",
                language="nl",
                content_kind="learning",
                age_suitability={"2": "SUITABLE", "6": "SUITABLE"},
                reason="profile API test",
                actor="test",
                correlation_id="profile-api-safety",
            )
        )

        profiles = client.get("/api/kids/profiles")
        assert profiles.status_code == 200
        assert {row["slug"] for row in profiles.json()["profiles"]} == {"noah", "felix"}

        changed = client.put(
            f"/api/kids/sources/{source_id}/profiles",
            json={
                "profile_slugs": ["felix"],
                "actor": "parent-test",
                "reason": "Assign source to Felix",
                "correlation_id": "profile-api-assignment",
            },
        )
        assert changed.status_code == 200
        assert changed.json()["profile_slugs"] == ["felix"]

        rows = client.get("/api/kids/sources", params={"profile": "felix"}).json()["sources"]
        assert [row["reference"] for row in rows] == ["UC-profile-api"]


def test_parent_can_correct_source_language_and_content_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        source = client.post(
            "/api/kids/sources",
            json={
                "kind": "channel",
                "reference": "UC-classification-api",
                "title": "Classification API",
            },
        ).json()
        changed = client.put(
            f"/api/kids/sources/{source['id']}/classification",
            json={
                "language": "en",
                "content_kind": "entertainment",
                "actor": "parent-test",
                "reason": "English cartoons",
                "correlation_id": "classification-api",
            },
        )

        assert changed.status_code == 200
        assert changed.json()["language"] == "en"
        assert changed.json()["content_kind"] == "entertainment"
        audit = client.get("/api/kids/audit").json()["events"][0]
        assert audit["event"] == "source_classification_changed"
        assert "en/entertainment" in audit["reason"]


def test_profile_avatar_can_be_uploaded_read_replaced_and_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        noah = client.get("/api/kids/profiles").json()["profiles"][0]
        assert noah["avatar_url"] is None

        jpeg = b"\xff\xd8\xff\xe0" + b"profile-photo"
        uploaded = client.put(
            "/api/kids/profiles/noah/avatar",
            content=jpeg,
            headers={"Content-Type": "image/jpeg"},
        )
        assert uploaded.status_code == 200
        avatar_url = uploaded.json()["profile"]["avatar_url"]
        assert avatar_url.startswith("http://testserver/api/kids/profiles/noah/avatar?v=")
        fetched = client.get(avatar_url)
        assert fetched.status_code == 200
        assert fetched.headers["content-type"] == "image/jpeg"
        assert fetched.content == jpeg
        assert avatar_url in client.get("/kids").text

        png = b"\x89PNG\r\n\x1a\n" + b"profile-photo"
        replaced = client.put("/api/kids/profiles/noah/avatar", content=png)
        assert replaced.status_code == 200
        assert replaced.json()["profile"]["avatar_url"] != avatar_url
        assert client.get(replaced.json()["profile"]["avatar_url"]).headers["content-type"] == "image/png"

        rejected = client.put("/api/kids/profiles/noah/avatar", content=b"not an image")
        assert rejected.status_code == 415

        kids_api = importlib.import_module("app.kids_api")
        monkeypatch.setattr(kids_api, "KIDS_PROFILE_AVATAR_MAX_BYTES", 8)
        oversized = client.put(
            "/api/kids/profiles/noah/avatar",
            content=b"\xff\xd8\xff" + b"x" * 6,
        )
        assert oversized.status_code == 413

        deleted = client.delete("/api/kids/profiles/noah/avatar")
        assert deleted.status_code == 200
        assert deleted.json()["profile"]["avatar_url"] is None
        assert client.get("/api/kids/profiles/noah/avatar").status_code == 404

        audit_events = client.get("/api/kids/audit").json()["events"]
        assert [event["event"] for event in audit_events[:3]] == [
            "profile_avatar_deleted",
            "profile_avatar_updated",
            "profile_avatar_updated",
        ]


def test_profile_names_are_normalized_and_playback_stays_profile_bound(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(1080,)))

    monkeypatch.setenv("SENTINEL_DB_PATH", str(db_path))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        noah_feed = client.get("/v1/kids/feed", params={"profile": "NoAh", "limit": 1})
        assert noah_feed.status_code == 200
        assert len(noah_feed.json()["items"]) == 1

        blocked = client.get(
            "/api/kids/playback-authorizations/dataplane-0",
            params={"profile": "FeLiX"},
        )
        assert blocked.status_code == 403

        source = asyncio.run(db.catalog_sources_list())[0]
        asyncio.run(
            db.kids_source_profiles_set(
                source["id"],
                ["felix"],
                actor="test",
                reason="Move source to Felix",
                correlation_id="profile-playback-isolation",
            )
        )

        assert client.get(
            "/api/kids/playback-authorizations/dataplane-0",
            params={"profile": "noah"},
        ).status_code == 403
        assert client.get(
            "/api/kids/playback-authorizations/dataplane-0",
            params={"profile": "FELIX"},
        ).status_code == 200
