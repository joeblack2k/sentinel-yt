import asyncio
import importlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import Database


async def seed_unassessed_ready_item(db_path: str) -> None:
    db = Database(db_path)
    await db.init()
    source = await db.catalog_create(
        "source",
        {
            "kind": "channel",
            "reference": "UC-readiness-unassessed",
            "title": "Readiness source",
            "profile_slugs": ["noah"],
            "correlation_id": "readiness-source",
        },
    )
    await db.catalog_source_safety_update(
        source["id"],
        verdict="SAFE",
        language="en",
        content_kind="learning",
        age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
        reason="safe source",
        actor="test",
        correlation_id="readiness-source-safety",
    )
    await db.catalog_transition(
        "source",
        source["id"],
        {
            "state": "approved",
            "actor": "test",
            "reason": "approved",
            "correlation_id": "readiness-source-approved",
        },
    )
    item = await db.catalog_create(
        "item",
        {
            "video_id": "ready000001",
            "title": "Unassessed item",
            "source_id": source["id"],
            "channel_id": "UC-readiness-unassessed",
            "channel_title": "Readiness source",
            "correlation_id": "readiness-item",
        },
    )
    await db.catalog_transition(
        "item",
        item["id"],
        {
            "state": "approved",
            "actor": "test",
            "reason": "approved",
            "correlation_id": "readiness-item-approved",
        },
    )
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    candidate = {
        "kind": "adaptive_mpv",
        "media_url": (
            "https://rr1---sn.example.googlevideo.com/videoplayback/video"
            f"?expire={int(expires_at.timestamp())}&sig=opaque"
        ),
        "audio_url": (
            "https://rr1---sn.example.googlevideo.com/videoplayback/audio"
            f"?expire={int(expires_at.timestamp())}&sig=opaque"
        ),
        "quality_height": 1080,
        "codec": "avc1.640028",
        "video_headers": {},
        "audio_headers": {},
    }
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE kids_resolve_backlog
            SET status='ready',candidate_json=?,quality_height=1080,
                codec='avc1.640028',resolved_at=?,expires_at=?
            WHERE item_id=?
            """,
            (
                json.dumps(candidate),
                datetime.now(timezone.utc).isoformat(),
                expires_at.isoformat(),
                item["id"],
            ),
        )
        connection.commit()


def test_kids_readyz_uses_resolver_inventory_when_ingest_is_deferred(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sentinel.db")
    monkeypatch.setenv("SENTINEL_DB_PATH", db_path)
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KIDS_INGEST_FRESHNESS_SECONDS", "1800")
    monkeypatch.setenv("KIDS_READY_MINIMUM", "0")
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        unverified = client.get("/api/kids/readyz")
        assert unverified.status_code == 503
        assert unverified.json()["detail"]["opencodex"] == "unavailable"

    asyncio.run(
        Database(db_path).set_setting(
            "kids_ingest_last_success_at",
            datetime.now(timezone.utc).isoformat(),
        )
    )
    asyncio.run(
        Database(db_path).set_setting(
            "kids_resolver_classifier_status",
            "ready",
        )
    )
    with TestClient(module.app) as client:
        ready = client.get("/api/kids/readyz")
        assert ready.status_code == 200
        assert ready.json()["opencodex"] == "ready"
        assert ready.json()["ingest"] == "fresh"
        assert client.get("/readyz").status_code == 200

    asyncio.run(
        Database(db_path).set_setting(
            "kids_resolver_classifier_status",
            "unavailable",
        )
    )
    with TestClient(module.app) as client:
        unavailable = client.get("/api/kids/readyz")
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"]["opencodex"] == "unavailable"


def test_kids_readyz_does_not_count_unassessed_ready_rows(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sentinel.db")
    monkeypatch.setenv("SENTINEL_DB_PATH", db_path)
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KIDS_READY_MINIMUM", "1")
    asyncio.run(seed_unassessed_ready_item(db_path))
    asyncio.run(
        Database(db_path).set_setting(
            "kids_resolver_classifier_status",
            "ready",
        )
    )
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        unavailable = client.get("/api/kids/readyz")

    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["fresh_ready_count"] == 0
