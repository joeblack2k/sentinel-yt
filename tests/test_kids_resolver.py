import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.services.kids_resolver import PRACTICAL_CANDIDATE_TTL, normalize_candidate, run_once


def adaptive_payload(quality_height: object) -> dict:
    resolved_at = datetime.now(timezone.utc)
    expiry = resolved_at + timedelta(hours=1)
    return {
        "status": "ready",
        "resolved_at": resolved_at.isoformat(),
        "expires_at": expiry.isoformat(),
        "candidate": {
            "media_url": signed_url("video", expiry),
            "audio_url": signed_url("audio", expiry),
            "quality_height": quality_height,
            "codec": "avc1.640028",
            "video_headers": {},
            "audio_headers": {},
        },
    }


def signed_url(kind: str, expires_at: datetime) -> str:
    return (
        f"https://rr1---sn.example.googlevideo.com/videoplayback/{kind}"
        f"?expire={int(expires_at.timestamp())}&sig=opaque"
    )


def test_minimum_quality_height_setting_is_configurable_and_fail_closed(monkeypatch):
    monkeypatch.delenv("KIDS_RESOLVER_MIN_QUALITY_HEIGHT", raising=False)
    assert Settings().kids_resolver_min_quality_height == 720

    monkeypatch.setenv("KIDS_RESOLVER_MIN_QUALITY_HEIGHT", "1080")
    assert Settings().kids_resolver_min_quality_height == 1080

    for value in ("360", "1081", "invalid"):
        monkeypatch.setenv("KIDS_RESOLVER_MIN_QUALITY_HEIGHT", value)
        assert Settings().kids_resolver_min_quality_height == 720


@pytest.mark.parametrize(
    ("quality_height", "accepted"),
    [(719, False), (720, True), (1080, True), (1081, False), (True, False), (720.0, False)],
)
def test_normalize_candidate_enforces_configured_quality_range(quality_height, accepted):
    normalized = normalize_candidate(adaptive_payload(quality_height))
    assert (normalized is not None) is accepted


def test_normalize_candidate_uses_configured_minimum():
    payload = adaptive_payload(720)
    assert normalize_candidate(payload, minimum_quality_height=720) is not None
    assert normalize_candidate(payload, minimum_quality_height=1080) is None
    assert normalize_candidate(payload, minimum_quality_height=360) is None


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


def test_progressive_muxed_candidate_is_rejected():
    resolved_at = datetime.now(timezone.utc)
    signed_expiry = resolved_at + timedelta(hours=1)
    normalized = normalize_candidate(
        {
            "status": "ready",
            "resolved_at": resolved_at.isoformat(),
            "expires_at": signed_expiry.isoformat(),
            "candidate": {
                "kind": "progressive_muxed",
                "media_url": signed_url("muxed", signed_expiry),
                "audio_url": None,
                "quality_height": 720,
                "codec": "avc1.42001e",
                "container": "mp4",
                "mime_type": "video/mp4",
                "video_headers": {},
                "audio_headers": {},
            },
        }
    )

    assert normalized is None
    assert normalize_candidate(
        {
            "status": "ready",
            "resolved_at": resolved_at.isoformat(),
            "expires_at": signed_expiry.isoformat(),
            "candidate": {
                "kind": "progressive_muxed",
                "media_url": signed_url("muxed", signed_expiry),
                "audio_url": signed_url("audio", signed_expiry),
                "quality_height": 720,
                "codec": "avc1.42001e",
                "video_headers": {},
                "audio_headers": {},
            },
        }
    ) is None


def test_unknown_candidate_transport_is_denied():
    resolved_at = datetime.now(timezone.utc)
    signed_expiry = resolved_at + timedelta(hours=1)
    assert normalize_candidate(
        {
            "status": "ready",
            "resolved_at": resolved_at.isoformat(),
            "expires_at": signed_expiry.isoformat(),
            "candidate": {
                "kind": "hls",
                "media_url": signed_url("muxed", signed_expiry),
                "audio_url": None,
                "quality_height": 720,
                "codec": "avc1.42001e",
                "video_headers": {},
                "audio_headers": {},
            },
        }
    ) is None


async def eligible_item(db: Database, video_id: str = "video-ready") -> dict:
    channel_id = f"UC-{video_id}"
    source = await db.catalog_create(
        "source",
        {
            "kind": "channel",
            "reference": channel_id,
            "title": "Approved channel",
            "correlation_id": "source",
        },
    )
    await db.catalog_source_safety_update(
        source["id"], verdict="SAFE", reason="safe", actor="guardian", correlation_id="safe"
    )
    await db.catalog_transition(
        "source", source["id"], {"state": "approved", "actor": "parent", "reason": "approved", "correlation_id": "source-state"}
    )
    item = await db.catalog_create(
        "item",
        {
            "video_id": video_id,
            "source_id": source["id"],
            "channel_id": channel_id,
            "channel_title": "Approved channel",
            "correlation_id": "item",
        },
    )
    await db.catalog_transition(
        "item", item["id"], {"state": "approved", "actor": "parent", "reason": "approved", "correlation_id": "item-state"}
    )
    return item


async def persist_ready_candidate(db: Database, item_id: int, quality_height: int = 1080) -> None:
    payload = adaptive_payload(quality_height)
    await db.kids_resolve_success(
        item_id=item_id,
        candidate=payload["candidate"],
        quality_height=quality_height,
        codec="avc1.640028",
        resolved_at=payload["resolved_at"],
        expires_at=payload["expires_at"],
    )


@pytest.mark.asyncio
async def test_catalog_and_playback_authorization_fail_closed_for_identity_and_safety(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()

    missing_identity_source = await db.catalog_create(
        "source",
        {
            "kind": "channel",
            "reference": "UC-missing-identity",
            "title": "Approved channel",
            "correlation_id": "missing-identity-source",
        },
    )
    await db.catalog_source_safety_update(
        missing_identity_source["id"],
        verdict="SAFE",
        reason="safe",
        actor="guardian",
        correlation_id="missing-identity-safe",
    )
    await db.catalog_transition(
        "source",
        missing_identity_source["id"],
        {
            "state": "approved",
            "actor": "parent",
            "reason": "approved",
            "correlation_id": "missing-identity-approve-source",
        },
    )
    missing_identity_item = await db.catalog_create(
        "item",
        {
            "video_id": "video-missing",
            "source_id": missing_identity_source["id"],
            "correlation_id": "missing-identity-item",
        },
    )
    await db.catalog_transition(
        "item",
        missing_identity_item["id"],
        {
            "state": "approved",
            "actor": "parent",
            "reason": "approved",
            "correlation_id": "missing-identity-approve-item",
        },
    )
    await persist_ready_candidate(db, missing_identity_item["id"])

    assert await db.catalog_items_list() == []
    assert await db.kids_eligible_feed_list(300) == []
    assert await db.kids_playback_authorization("video-missing", minimum_remaining_seconds=300) is None
    assert await db.kids_playback_policy_authorization("video-missing") is None

    uncertain_source = await db.catalog_create(
        "source",
        {
            "kind": "channel",
            "reference": "UC-uncertain",
            "title": "Approved channel",
            "correlation_id": "uncertain-source",
        },
    )
    await db.catalog_source_safety_update(
        uncertain_source["id"],
        verdict="SAFE",
        reason="safe",
        actor="guardian",
        correlation_id="uncertain-safe",
    )
    await db.catalog_transition(
        "source",
        uncertain_source["id"],
        {
            "state": "approved",
            "actor": "parent",
            "reason": "approved",
            "correlation_id": "uncertain-approve-source",
        },
    )
    uncertain_item = await db.catalog_create(
        "item",
        {
            "video_id": "video-uncertain",
            "source_id": uncertain_source["id"],
            "channel_id": "UC-uncertain",
            "channel_title": "Approved channel",
            "correlation_id": "uncertain-item",
        },
    )
    await db.catalog_transition(
        "item",
        uncertain_item["id"],
        {
            "state": "approved",
            "actor": "parent",
            "reason": "approved",
            "correlation_id": "uncertain-approve-item",
        },
    )
    await persist_ready_candidate(db, uncertain_item["id"])
    await db.catalog_source_safety_update(
        uncertain_source["id"],
        verdict="UNCERTAIN",
        reason="unknown",
        actor="guardian",
        correlation_id="uncertain-revoked",
    )

    assert await db.catalog_items_list() == []
    assert await db.kids_eligible_feed_list(300) == []
    assert await db.kids_playback_authorization("video-uncertain", minimum_remaining_seconds=300) is None
    assert await db.kids_playback_policy_authorization("video-uncertain") is None


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
async def test_resolver_worker_requests_adaptive_transport(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    item = await eligible_item(db, "video-adaptive")
    expiry = datetime.now(timezone.utc) + timedelta(minutes=10)
    payload = {
        "youtube_id": "video-adaptive",
        "status": "ready",
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expiry.isoformat(),
        "candidate": {
            "media_url": signed_url("video", expiry),
            "audio_url": signed_url("audio", expiry),
            "quality_height": 720,
            "codec": "avc1.4d401f",
            "video_headers": {},
            "audio_headers": {},
        },
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        db_path=db.db_path,
        kids_resolver_backend_url="http://backend",
        kids_resolver_batch_size=1,
    )
    try:
        result = await run_once(db=db, settings=settings, client=client)
    finally:
        await client.aclose()

    assert result["ready"] == 1
    assert requests and dict(httpx.QueryParams(requests[0].url.query)) == {
        "target_height": "1080",
        "transport": "adaptive",
    }
    auth = await db.kids_playback_authorization("video-adaptive", minimum_remaining_seconds=300)
    assert auth and auth["candidate"]["quality_height"] == 720


@pytest.mark.asyncio
async def test_resolver_worker_retries_underquality_candidate(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    await eligible_item(db, "video-underquality")
    payload = adaptive_payload(360)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    settings = Settings(
        db_path=db.db_path,
        kids_resolver_backend_url="http://backend",
        kids_resolver_batch_size=1,
    )
    try:
        result = await run_once(db=db, settings=settings, client=client)
    finally:
        await client.aclose()

    assert result["ready"] == 0
    assert result["retry"] == 1
    assert result["invalid_candidate"] == 1
    assert await db.kids_playback_authorization("video-underquality", minimum_remaining_seconds=300) is None
    row = (await db.kids_resolve_recent_rows())[0]
    assert row["status"] == "retry"
    assert row["quality_height"] is None


@pytest.mark.asyncio
async def test_resolver_worker_requeues_legacy_360p_ready_row(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    item = await eligible_item(db, "video-legacy-360")
    old = adaptive_payload(360)
    await db.kids_resolve_success(
        item_id=item["id"],
        candidate=old["candidate"],
        quality_height=360,
        codec="avc1.640028",
        resolved_at=old["resolved_at"],
        expires_at=old["expires_at"],
    )
    assert (await db.kids_resolve_recent_rows())[0]["status"] == "ready"

    requests: list[httpx.Request] = []
    payload = adaptive_payload(720)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(
        db_path=db.db_path,
        kids_resolver_backend_url="http://backend",
        kids_resolver_batch_size=1,
    )
    try:
        result = await run_once(db=db, settings=settings, client=client)
    finally:
        await client.aclose()

    assert result["claimed"] == 1
    assert result["ready"] == 1
    assert len(requests) == 1
    row = (await db.kids_resolve_recent_rows())[0]
    assert row["status"] == "ready"
    assert row["quality_height"] == 720


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
