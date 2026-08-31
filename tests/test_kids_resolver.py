import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.services.kids_resolver import PRACTICAL_CANDIDATE_TTL, normalize_candidate, run_once


def signed_url(kind: str, expires_at: datetime) -> str:
    return (
        f"https://rr1---sn.example.googlevideo.com/videoplayback/{kind}"
        f"?expire={int(expires_at.timestamp())}&sig=opaque"
    )


def test_resolved_candidate_is_capped_before_po_token_goes_stale():
    resolved_at = datetime.now(timezone.utc)
    signed_expiry = resolved_at + timedelta(hours=6)
    normalized = normalize_candidate(
        {
            "status": "ready",
            "resolved_at": resolved_at.isoformat(),
            "expires_at": signed_expiry.isoformat(),
            "candidate": {
                "media_url": signed_url("video", signed_expiry),
                "audio_url": signed_url("audio", signed_expiry),
                "quality_height": 1080,
                "codec": "avc1.640028",
                "video_headers": {},
                "audio_headers": {},
            },
        }
    )

    assert normalized is not None
    practical_expiry = datetime.fromisoformat(normalized[4])
    assert practical_expiry == resolved_at + PRACTICAL_CANDIDATE_TTL


async def eligible_item(db: Database, video_id: str = "video-ready") -> dict:
    source = await db.catalog_create(
        "source", {"kind": "channel", "reference": f"UC-{video_id}", "correlation_id": "source"}
    )
    await db.catalog_source_safety_update(
        source["id"], verdict="SAFE", reason="safe", actor="guardian", correlation_id="safe"
    )
    await db.catalog_transition(
        "source", source["id"], {"state": "approved", "actor": "parent", "reason": "approved", "correlation_id": "source-state"}
    )
    item = await db.catalog_create(
        "item", {"video_id": video_id, "source_id": source["id"], "correlation_id": "item"}
    )
    await db.catalog_transition(
        "item", item["id"], {"state": "approved", "actor": "parent", "reason": "approved", "correlation_id": "item-state"}
    )
    return item


@pytest.mark.asyncio
async def test_resolver_queue_persists_success_expiry_backoff_and_bounded_claim(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    first = await eligible_item(db, "video-one")
    await eligible_item(db, "video-two")
    await eligible_item(db, "video-three")

    claimed = await db.kids_resolve_claim_due(limit=2, refresh_margin_seconds=1800)
    assert [row["video_id"] for row in claimed] == ["video-one", "video-two"]
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    candidate = {
        "media_url": signed_url("video", datetime.now(timezone.utc) + timedelta(minutes=10)),
        "audio_url": signed_url("audio", datetime.now(timezone.utc) + timedelta(minutes=10)),
        "quality_height": 720,
        "codec": "avc1.4d401f",
        "video_headers": {},
        "audio_headers": {},
    }
    await db.kids_resolve_success(
        item_id=first["id"],
        candidate=candidate,
        quality_height=720,
        codec="avc1.4d401f",
        resolved_at=datetime.now(timezone.utc).isoformat(),
        expires_at=expires_at,
    )
    assert (await db.kids_resolve_summary())["fresh_ready"] == 1
    await db.kids_resolve_failure(item_id=claimed[1]["item_id"], reason_code="backend_unavailable")
    rows = await db.kids_resolve_recent_rows()
    retry = next(row for row in rows if row["item_id"] == claimed[1]["item_id"])
    assert retry["status"] == "retry"
    assert retry["attempt_count"] == 1
    assert retry["last_error_code"] == "backend_unavailable"
    assert "candidate_json" not in retry
    refresh_claim = await db.kids_resolve_claim_due(limit=1, refresh_margin_seconds=1800)
    assert refresh_claim == [{"item_id": first["id"], "video_id": "video-one"}]
    assert await db.kids_playback_authorization("video-one", minimum_remaining_seconds=300)
    assert [item["video_id"] for item in await db.kids_eligible_feed_list(300)] == ["video-one"]


@pytest.mark.asyncio
async def test_resolver_worker_keeps_candidate_scoped_to_authorization_storage(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    item = await eligible_item(db)
    expiry = datetime.now(timezone.utc) + timedelta(minutes=10)
    payload = {
        "youtube_id": "video-ready",
        "status": "ready",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expiry.isoformat(),
        "candidate": {
            "media_url": signed_url("video", expiry),
            "audio_url": signed_url("audio", expiry),
            "quality_height": 1080,
            "codec": "avc1.640028",
            "video_headers": {"Origin": "https://www.youtube.com"},
            "audio_headers": {},
        },
    }
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    settings = Settings(
        db_path=db.db_path,
        kids_resolver_backend_url="http://backend",
        kids_resolver_batch_size=2,
    )
    try:
        result = await run_once(db=db, settings=settings, client=client)
    finally:
        await client.aclose()
    assert result["ready"] == 1
    assert await db.get_setting("kids_resolver_last_success_at")
    auth = await db.kids_playback_authorization("video-ready", minimum_remaining_seconds=300)
    assert auth and auth["item_id"] == item["id"]
    assert auth["candidate"]["media_url"].startswith("https://")
    assert (await db.kids_eligible_feed_list(300))[0]["video_id"] == "video-ready"
    await db.catalog_source_safety_update(
        1, verdict="UNSAFE", reason="revoked", actor="guardian", correlation_id="unsafe"
    )
    assert await db.kids_playback_authorization("video-ready", minimum_remaining_seconds=300) is None


@pytest.mark.asyncio
async def test_feed_orders_newly_probed_candidates_first(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    older = await eligible_item(db, "video-older")
    newer = await eligible_item(db, "video-newer")
    await db.kids_resolve_claim_due(limit=2, refresh_margin_seconds=300)
    expiry = datetime.now(timezone.utc) + timedelta(minutes=15)
    candidate = {
        "media_url": signed_url("video", expiry),
        "audio_url": signed_url("audio", expiry),
        "quality_height": 1080,
        "codec": "avc1.640028",
        "video_headers": {},
        "audio_headers": {},
    }
    await db.kids_resolve_success(
        item_id=older["id"],
        candidate=candidate,
        quality_height=1080,
        codec="avc1.640028",
        resolved_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        expires_at=expiry.isoformat(),
    )
    await db.kids_resolve_success(
        item_id=newer["id"],
        candidate=candidate,
        quality_height=1080,
        codec="avc1.640028",
        resolved_at=datetime.now(timezone.utc).isoformat(),
        expires_at=expiry.isoformat(),
    )

    feed = await db.kids_eligible_feed_list(300)
    assert [item["video_id"] for item in feed] == ["video-newer", "video-older"]


@pytest.mark.asyncio
async def test_active_policy_authorization_survives_resolver_retry_but_not_revoke(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    item = await eligible_item(db, "video-active")
    claimed = await db.kids_resolve_claim_due(limit=1, refresh_margin_seconds=300)
    assert claimed == [{"item_id": item["id"], "video_id": "video-active"}]
    await db.kids_resolve_failure(item_id=item["id"], reason_code="backend_unavailable")

    assert await db.kids_playback_authorization("video-active", minimum_remaining_seconds=300) is None
    assert await db.kids_playback_policy_authorization("video-active") == {
        "item_id": item["id"],
        "video_id": "video-active",
    }

    await db.catalog_transition(
        "item",
        item["id"],
        {
            "state": "revoked",
            "actor": "parent",
            "reason": "revoked",
            "correlation_id": "item-revoked",
        },
    )
    assert await db.kids_playback_policy_authorization("video-active") is None


def test_playback_revalidation_can_omit_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    import importlib

    module = importlib.reload(importlib.import_module("app.main"))
    row = {
        "item_id": 7,
        "video_id": "video-ready",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "quality_height": 1080,
        "codec": "avc1.640028",
        "candidate": {"media_url": "https://media.example/video"},
    }

    async def available() -> bool:
        return True

    async def disabled() -> bool:
        return False

    async def authorize(video_id: str, *, minimum_remaining_seconds: int):
        assert video_id == "video-ready"
        assert minimum_remaining_seconds > 0
        return row

    async def policy_authorize(video_id: str):
        assert video_id == "video-ready"
        return {"item_id": 7, "video_id": "video-ready"}

    async def revision() -> int:
        return 12

    with TestClient(module.app) as client:
        runtime = client.app.state.runtime
        monkeypatch.setattr(runtime, "monitoring_enabled_now", available)
        monkeypatch.setattr(runtime.db, "kids_kill_switch_enabled", disabled)
        monkeypatch.setattr(runtime.db, "kids_playback_authorization", authorize)
        monkeypatch.setattr(runtime.db, "kids_playback_policy_authorization", policy_authorize)
        monkeypatch.setattr(runtime.db, "catalog_revision", revision)

        initial = client.get("/api/kids/playback-authorizations/video-ready")
        assert initial.status_code == 200
        assert initial.json()["candidate"] == row["candidate"]

        recheck = client.get(
            "/api/kids/playback-authorizations/video-ready",
            params={"include_candidate": "false"},
        )
        assert recheck.status_code == 200
        assert "candidate" not in recheck.json()
