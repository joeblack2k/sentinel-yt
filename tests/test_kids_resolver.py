import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.services.kids_resolver import (
    KIDS_WATCH_URL,
    PRACTICAL_CANDIDATE_TTL,
    YtDlpResolver,
    candidate_expiry,
    normalize_candidate,
    run_once,
    select_candidate,
)


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


def extraction_dump(*, height: int = 1080, width: int = 1920, minutes: int = 60) -> dict:
    expiry = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return {
        "width": width,
        "height": height,
        "formats": [
            {
                "url": signed_url("audio", expiry),
                "vcodec": "none",
                "acodec": "mp4a.40.2",
                "abr": 128,
                "http_headers": {"User-Agent": "test", "Cookie": "never-forward"},
            },
            {
                "url": signed_url(f"video-{height}", expiry),
                "vcodec": "avc1.640028",
                "acodec": "none",
                "height": height,
                "width": width,
                "tbr": 4000,
                "http_headers": {"User-Agent": "test"},
            },
        ],
    }


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


async def approved_item_for_source(
    db: Database,
    source_id: int,
    channel_id: str,
    video_id: str,
) -> dict:
    item = await db.catalog_create(
        "item",
        {
            "video_id": video_id,
            "source_id": source_id,
            "channel_id": channel_id,
            "channel_title": "Approved channel",
            "correlation_id": "item",
        },
    )
    await db.catalog_transition(
        "item",
        item["id"],
        {"state": "approved", "actor": "parent", "reason": "approved", "correlation_id": "item-state"},
    )
    return item


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
        source["id"],
        verdict="SAFE",
        language="nl",
        age_suitability={"2": "SUITABLE", "6": "SUITABLE"},
        reason="safe",
        actor="guardian",
        correlation_id="safe",
    )
    await db.catalog_transition(
        "source", source["id"], {"state": "approved", "actor": "parent", "reason": "approved", "correlation_id": "source-state"}
    )
    return await approved_item_for_source(db, source["id"], channel_id, video_id)


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
async def test_playback_authorization_requires_explicit_age_suitability(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    source = await db.catalog_create(
        "source",
        {
            "kind": "channel",
            "reference": "UC-without-age-policy",
            "title": "Missing age policy",
            "correlation_id": "source-without-age-policy",
        },
    )
    await db.catalog_source_safety_update(
        source["id"],
        verdict="SAFE",
        language="nl",
        reason="legacy decision without age policy",
        actor="guardian",
        correlation_id="safe-without-age-policy",
    )
    await db.catalog_transition(
        "source",
        source["id"],
        {
            "state": "approved",
            "actor": "parent",
            "reason": "approved",
            "correlation_id": "approve-source-without-age-policy",
        },
    )
    item = await approved_item_for_source(
        db,
        source["id"],
        "UC-without-age-policy",
        "video-without-age-policy",
    )
    await persist_ready_candidate(db, item["id"])

    assert (
        await db.kids_playback_authorization(
            "video-without-age-policy",
            profile="noah",
            minimum_remaining_seconds=300,
        )
        is None
    )
    assert (
        await db.kids_playback_policy_authorization(
            "video-without-age-policy",
            profile="noah",
        )
        is None
    )


@pytest.mark.asyncio
async def test_resolver_queue_persists_success_expiry_backoff_and_bounded_claim(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    first = await eligible_item(db, "video-one")
    await eligible_item(db, "video-two")
    third = await eligible_item(db, "video-three")

    claimed = await db.kids_resolve_claim_due(limit=2, refresh_margin_seconds=1800)
    assert [row["video_id"] for row in claimed] == ["video-one", "video-two"]
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    candidate = {
        "media_url": signed_url("video", datetime.now(timezone.utc) + timedelta(hours=6)),
        "audio_url": signed_url("audio", datetime.now(timezone.utc) + timedelta(hours=6)),
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
    next_claim = await db.kids_resolve_claim_due(limit=1, refresh_margin_seconds=1800)
    assert next_claim == [{"item_id": third["id"], "video_id": "video-three"}]
    assert await db.kids_playback_authorization("video-one", minimum_remaining_seconds=300)
    assert [item["video_id"] for item in await db.kids_eligible_feed_list(300)] == ["video-one"]


@pytest.mark.asyncio
async def test_backlog_sync_extends_legacy_ready_expiry_within_signed_limits(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    item = await eligible_item(db, "video-legacy-ttl")
    resolved_at = datetime.now(timezone.utc)
    signed_expiry = resolved_at + timedelta(hours=6)
    candidate = {
        "media_url": signed_url("video", signed_expiry),
        "audio_url": signed_url("audio", signed_expiry),
        "quality_height": 720,
        "codec": "avc1.4d401f",
        "video_headers": {},
        "audio_headers": {},
    }
    await db.kids_resolve_success(
        item_id=item["id"],
        candidate=candidate,
        quality_height=720,
        codec="avc1.4d401f",
        resolved_at=resolved_at.isoformat(),
        expires_at=signed_expiry.isoformat(),
    )
    with sqlite3.connect(db.db_path) as connection:
        connection.execute(
            "UPDATE kids_resolve_backlog SET expires_at=? WHERE item_id=?",
            ((resolved_at + timedelta(minutes=20)).isoformat(), item["id"]),
        )
        connection.commit()

    await db.kids_resolve_sync_backlog(minimum_quality_height=720)

    with sqlite3.connect(db.db_path) as connection:
        stored_expiry = connection.execute(
            "SELECT expires_at FROM kids_resolve_backlog WHERE item_id=?",
            (item["id"],),
        ).fetchone()[0]
    assert datetime.fromisoformat(stored_expiry) == resolved_at + PRACTICAL_CANDIDATE_TTL


@pytest.mark.asyncio
async def test_resolver_claim_interleaves_unresolved_sources(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    first = await eligible_item(db, "video-source-a-1")
    same_source = await approved_item_for_source(
        db,
        first["source_id"],
        first["channel_id"],
        "video-source-a-2",
    )
    other_source = await eligible_item(db, "video-source-b-1")

    claimed = await db.kids_resolve_claim_due(limit=2, refresh_margin_seconds=300)

    assert {row["item_id"] for row in claimed} == {first["id"], other_source["id"]}
    assert same_source["id"] not in {row["item_id"] for row in claimed}


@pytest.mark.asyncio
async def test_resolver_claim_prioritizes_sources_used_by_profile_shelves(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    await eligible_item(db, "video-generic")
    shelf_item = await eligible_item(db, "video-shelf")
    await db.catalog_source_classification_update(
        shelf_item["source_id"],
        language="nl",
        content_kind="learning",
        age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
        actor="guardian",
        reason="resolver shelf priority test",
        correlation_id="resolver-shelf-priority",
    )

    claimed = await db.kids_resolve_claim_due(limit=1, refresh_margin_seconds=300)

    assert claimed == [
        {"item_id": shelf_item["id"], "video_id": shelf_item["video_id"]}
    ]


@pytest.mark.asyncio
async def test_resolver_claim_prioritizes_materialized_daily_items(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    daily_item = await eligible_item(db, "video-daily")
    shelf_item = await eligible_item(db, "video-shelf")
    await db.catalog_source_classification_update(
        shelf_item["source_id"],
        language="nl",
        content_kind="learning",
        actor="guardian",
        reason="resolver shelf priority test",
        correlation_id="resolver-shelf-priority",
    )
    await db.kids_daily_library_get_or_create(
        day="2026-09-04",
        profile="noah",
        shelf_limit=1,
        proposed_item_ids={"learning": [daily_item["id"]]},
    )

    claimed = await db.kids_resolve_claim_due(limit=1, refresh_margin_seconds=300)

    assert claimed == [
        {"item_id": daily_item["id"], "video_id": daily_item["video_id"]}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("height", "width", "accepted"),
    [(720, 1280, True), (1080, 1920, True), (360, 640, False), (2160, 3840, False)],
)
async def test_local_resolver_enforces_adaptive_quality_ceiling(height, width, accepted):
    candidate = select_candidate(extraction_dump(height=height, width=width))
    assert (candidate is not None) is accepted
    if candidate is not None:
        assert candidate["quality_height"] in {720, 1080}
        assert candidate["kind"] == "adaptive_mpv"
        assert candidate_expiry(candidate) is not None


@pytest.mark.asyncio
async def test_resolver_is_anonymous_first_and_only_falls_back_when_usable():
    cookie_calls = 0

    async def cookies():
        nonlocal cookie_calls
        cookie_calls += 1
        return [{"domain": ".youtube.com", "name": "SID", "value": "secret"}]

    class Resolver(YtDlpResolver):
        def __init__(self, responses):
            super().__init__(cookie_provider=cookies)
            self.responses = iter(responses)
            self.calls = []

        async def _extract(self, video_id, *, cookies=None):
            self.calls.append(cookies is not None)
            return next(self.responses)

    resolver = Resolver([extraction_dump()])
    candidate = await resolver.resolve("abcdefghijk")
    assert candidate and candidate["quality_height"] == 1080
    assert resolver.calls == [False]
    assert cookie_calls == 0

    resolver = Resolver([extraction_dump(height=1920, width=1080)])
    assert await resolver.resolve("bcdefghijkl") is None
    assert resolver.calls == [False]
    assert cookie_calls == 0

    resolver = Resolver([{"formats": []}, extraction_dump(height=720, width=1280)])
    candidate = await resolver.resolve("cdefghijklm")
    assert candidate and candidate["quality_height"] == 720
    assert resolver.calls == [False, True]
    assert cookie_calls == 1


@pytest.mark.asyncio
async def test_ytdlp_extract_uses_kids_watch_url_and_deletes_cookie_file(monkeypatch):
    commands = []
    cookie_texts = []

    class Process:
        returncode = 0

        def __init__(self, command):
            self.command = command

        async def communicate(self):
            if "--cookies" in self.command:
                path = Path(self.command[self.command.index("--cookies") + 1])
                cookie_texts.append(path.read_text(encoding="utf-8"))
            return b'{"formats": []}\n', b""

        async def wait(self):
            return None

    async def create_process(*command, **kwargs):
        commands.append(command)
        return Process(command)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)

    async def cookies():
        return []

    resolver = YtDlpResolver(cookie_provider=cookies)
    await resolver._extract("abcdefghijk")
    await resolver._extract(
        "bcdefghijkl",
        cookies=[{"domain": ".youtube.com", "name": "SID", "value": "secret"}],
    )

    assert commands[0][-1] == f"{KIDS_WATCH_URL}abcdefghijk"
    assert commands[1][-1] == f"{KIDS_WATCH_URL}bcdefghijkl"
    cookie_arg = commands[1][commands[1].index("--cookies") + 1]
    assert not Path(cookie_arg).exists()
    assert "SID\tsecret" in cookie_texts[0]


@pytest.mark.asyncio
async def test_resolver_timeout_covers_anonymous_and_authenticated_attempts():
    calls = []
    cookie_calls = 0

    async def cookies():
        nonlocal cookie_calls
        cookie_calls += 1
        return [{"domain": ".youtube.com", "name": "SID", "value": "secret"}]

    class Resolver(YtDlpResolver):
        async def _extract(self, video_id, *, cookies=None):
            calls.append(cookies is not None)
            if cookies is not None:
                await asyncio.sleep(1)
            return {"formats": []}

    resolver = Resolver(cookie_provider=cookies, timeout_seconds=0.01)
    assert await resolver.resolve("defghijklmn") is None
    assert calls == [False, True]
    assert cookie_calls == 1


@pytest.mark.asyncio
async def test_resolver_timeout_kills_active_extractor_process(monkeypatch):
    class Process:
        returncode = None

        def __init__(self):
            self.killed = False
            self.waited = False

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True

    process = Process()

    async def create_process(*args, **kwargs):
        return process

    async def cookies():
        return []

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    resolver = YtDlpResolver(cookie_provider=cookies, timeout_seconds=0.01)

    assert await resolver.resolve("efghijklmno") is None
    assert process.killed and process.waited


@pytest.mark.asyncio
async def test_resolver_worker_keeps_candidate_scoped_to_authorization_storage(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    item = await eligible_item(db)
    settings = Settings(
        db_path=db.db_path,
        kids_resolver_batch_size=2,
    )

    class Resolver:
        async def resolve(self, video_id):
            assert video_id == "video-ready"
            return select_candidate(extraction_dump())

    result = await run_once(db=db, settings=settings, resolver=Resolver())
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
async def test_resolver_worker_enforces_configured_minimum_for_selection_and_persistence(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    await eligible_item(db, "video-720")
    await eligible_item(db, "video-1080")
    settings = Settings(
        db_path=db.db_path,
        kids_resolver_batch_size=2,
        kids_resolver_min_quality_height=1080,
    )

    class Resolver:
        async def resolve(self, video_id):
            height = 720 if video_id == "video-720" else 1080
            return select_candidate(extraction_dump(height=height, width=1280 if height == 720 else 1920))

    result = await run_once(db=db, settings=settings, resolver=Resolver())

    assert result["claimed"] == 2
    assert result["ready"] == 1
    assert result["retry"] == 1
    assert result["no_compatible_stream"] == 1
    rows = {row["video_id"]: row for row in await db.kids_resolve_recent_rows()}
    assert rows["video-720"]["status"] == "retry"
    assert rows["video-720"]["last_error_code"] == "no_compatible_stream"
    assert rows["video-1080"]["status"] == "ready"
    assert rows["video-1080"]["quality_height"] == 1080


@pytest.mark.asyncio
async def test_kids_resolve_success_rejects_quality_below_configured_minimum(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    item = await eligible_item(db, "video-direct-quality")
    payload = adaptive_payload(720)

    await db.kids_resolve_success(
        item_id=item["id"],
        candidate=payload["candidate"],
        quality_height=720,
        codec="avc1.640028",
        resolved_at=payload["resolved_at"],
        expires_at=payload["expires_at"],
        minimum_quality_height=1080,
    )

    row = next(
        row for row in await db.kids_resolve_recent_rows() if row["item_id"] == item["id"]
    )
    assert row["status"] == "pending"
    assert row["quality_height"] is None


@pytest.mark.asyncio
async def test_resolver_worker_retries_underquality_candidate(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    await eligible_item(db, "video-underquality")
    settings = Settings(
        db_path=db.db_path,
        kids_resolver_batch_size=1,
    )

    class Resolver:
        async def resolve(self, video_id):
            return None

    result = await run_once(db=db, settings=settings, resolver=Resolver())
    assert result["ready"] == 0
    assert result["retry"] == 1
    assert result["no_compatible_stream"] == 1
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
    with sqlite3.connect(db.db_path) as connection:
        connection.execute(
            """
            UPDATE kids_resolve_backlog
            SET status='ready', candidate_json=?, quality_height=360, codec=?,
                resolved_at=?, expires_at=?, next_attempt_at=NULL, last_error_code=''
            WHERE item_id=?
            """,
            (
                json.dumps(old["candidate"]),
                "avc1.640028",
                old["resolved_at"],
                old["expires_at"],
                item["id"],
            ),
        )
        connection.commit()
    assert (await db.kids_resolve_recent_rows())[0]["status"] == "ready"
    settings = Settings(
        db_path=db.db_path,
        kids_resolver_batch_size=1,
    )

    class Resolver:
        async def resolve(self, video_id):
            return select_candidate(extraction_dump(height=720, width=1280))

    result = await run_once(db=db, settings=settings, resolver=Resolver())
    assert result["claimed"] == 1
    assert result["ready"] == 1
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
    expiry = datetime.now(timezone.utc) + timedelta(hours=6)
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
async def test_feed_interleaves_sources_without_losing_freshness(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    first_source_item = await eligible_item(db, "video-source-a-1")
    second_source_item = await eligible_item(db, "video-source-b-1")
    extra_item = await approved_item_for_source(
        db,
        first_source_item["source_id"],
        first_source_item["channel_id"],
        "video-source-a-2",
    )
    for item in (first_source_item, second_source_item, extra_item):
        await persist_ready_candidate(db, item["id"])

    feed = await db.kids_eligible_feed_list(300)
    source_ids = [item["source_id"] for item in feed]
    assert source_ids == [
        first_source_item["source_id"],
        second_source_item["source_id"],
        first_source_item["source_id"],
    ]


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

    async def authorize(video_id: str, *, profile: str, minimum_remaining_seconds: int):
        assert video_id == "video-ready"
        assert profile == "noah"
        assert minimum_remaining_seconds > 0
        return row

    async def policy_authorize(video_id: str, *, profile: str):
        assert video_id == "video-ready"
        assert profile == "noah"
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
