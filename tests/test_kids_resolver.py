import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import Settings
from app.db import Database
from app.services.kids_resolver import run_once


def signed_url(kind: str, expires_at: datetime) -> str:
    return (
        f"https://rr1---sn.example.googlevideo.com/videoplayback/{kind}"
        f"?expire={int(expires_at.timestamp())}&sig=opaque"
    )


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
