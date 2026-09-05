import asyncio
import importlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db import Database


CHANNEL_ID = f"UC{'a' * 22}"
SECONDARY_CHANNEL_ID = f"UC{'b' * 22}"
GENUINE_AVATAR = "https://yt3.ggpht.com/channel/avatar=s176-c-k-c0x00ffffff-no-rj"


def signed_url(kind: str, expires_at: datetime) -> str:
    return (
        f"https://rr1---sn.example.googlevideo.com/videoplayback/{kind}"
        f"?expire={int(expires_at.timestamp())}&sig=signature"
    )


async def seed_catalog(db_path, *, qualities=(720, 1080)) -> Database:
    db = Database(str(db_path))
    await db.init()
    await db.set_setting("kids_kill_switch", "false")
    schedule = (await db.list_schedules())[0]
    await db.update_schedule(
        int(schedule["id"]),
        name="Always open",
        enabled=True,
        start="00:00",
        end="00:00",
        timezone="UTC",
        mode="blocklist",
    )
    source = await db.catalog_create(
        "source",
        {
            "kind": "channel",
            "reference": CHANNEL_ID,
            "title": "Sentinel test source",
            "avatar_url": GENUINE_AVATAR,
            "correlation_id": "dataplane-source",
        },
    )
    await db.catalog_source_safety_update(
        source["id"],
        verdict="SAFE",
        reason="contract test",
        actor="test",
        correlation_id="dataplane-safety",
        policy_version="test-v1",
        language="nl",
        age_suitability={"2": "SUITABLE", "6": "SUITABLE"},
    )
    source = await db.catalog_transition(
        "source",
        source["id"],
        {
            "state": "approved",
            "actor": "test",
            "reason": "contract test",
            "correlation_id": "dataplane-source-approved",
        },
    )
    for index, quality in enumerate(qualities):
        item = await db.catalog_create(
            "item",
            {
                "video_id": f"dataplane-{index}",
                "title": f"Test item {index}",
                "source_id": source["id"],
                "channel_id": CHANNEL_ID,
                "channel_title": "Sentinel test source",
                "thumbnail_url": f"https://i.ytimg.com/vi/dataplane-{index}/hqdefault.jpg",
                "duration_seconds": 42 + index,
                "visual_category": "educational",
                "correlation_id": f"dataplane-item-{index}",
            },
        )
        await db.catalog_transition(
            "item",
            item["id"],
            {
                "state": "approved",
                "actor": "test",
                "reason": "contract test",
                "correlation_id": f"dataplane-item-approved-{index}",
            },
        )
        await db.catalog_item_safety_update(
            item["id"],
            verdict="SAFE",
            language="nl",
            content_kind="learning",
            age_suitability={"2": "SUITABLE", "6": "SUITABLE"},
            reason="contract item safety",
            actor="test",
            correlation_id=f"dataplane-item-safety-{index}",
        )
        expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        candidate = {
            "kind": "adaptive_mpv",
            "media_url": signed_url("video", expires_at),
            "audio_url": signed_url("audio", expires_at),
            "quality_height": quality,
            "codec": "avc1.640028",
            "video_headers": {
                "User-Agent": "sentinel-test",
                "Cookie": "must-not-forward",
            },
            "audio_headers": {},
        }
        await db.kids_resolve_success(
            item_id=item["id"],
            candidate=candidate,
            quality_height=quality,
            codec="avc1.640028",
            resolved_at=datetime.now(timezone.utc).isoformat(),
            expires_at=expires_at.isoformat(),
        )
    return db


async def add_ready_source_item(
    db: Database,
    *,
    suffix: str,
    kind: str = "channel",
    reference: str = SECONDARY_CHANNEL_ID,
    safety_verdict: str = "SAFE",
) -> tuple[dict, dict]:
    source = await db.catalog_create(
        "source",
        {
            "kind": kind,
            "reference": reference,
            "title": f"Secondary {suffix}",
            "avatar_url": GENUINE_AVATAR,
            "correlation_id": f"secondary-source-{suffix}",
        },
    )
    await db.catalog_source_safety_update(
        source["id"],
        verdict=safety_verdict,
        reason=f"secondary {suffix}",
        actor="test",
        correlation_id=f"secondary-safety-{suffix}",
        policy_version="test-v1",
        language="nl",
        age_suitability={"2": "SUITABLE", "6": "SUITABLE"},
    )
    source = await db.catalog_transition(
        "source",
        source["id"],
        {
            "state": "approved",
            "actor": "test",
            "reason": f"secondary {suffix}",
            "correlation_id": f"secondary-source-approved-{suffix}",
        },
    )
    item = await db.catalog_create(
        "item",
        {
            "video_id": f"secondary-{suffix}",
            "title": f"Secondary item {suffix}",
            "source_id": source["id"],
            "channel_id": reference,
            "channel_title": source["title"],
            "correlation_id": f"secondary-item-{suffix}",
        },
    )
    item = await db.catalog_transition(
        "item",
        item["id"],
        {
            "state": "approved",
            "actor": "test",
            "reason": f"secondary {suffix}",
            "correlation_id": f"secondary-item-approved-{suffix}",
        },
    )
    await db.catalog_item_safety_update(
        item["id"],
        verdict="SAFE" if safety_verdict == "SAFE" else "UNCERTAIN",
        language="nl",
        content_kind="learning",
        age_suitability={"2": "SUITABLE", "6": "SUITABLE"},
        reason=f"secondary item {suffix}",
        actor="test",
        correlation_id=f"secondary-item-safety-{suffix}",
    )
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    await db.kids_resolve_success(
        item_id=item["id"],
        candidate={
            "kind": "adaptive_mpv",
            "media_url": signed_url("video", expires_at),
            "audio_url": signed_url("audio", expires_at),
            "quality_height": 720,
            "codec": "avc1.640028",
        },
        quality_height=720,
        codec="avc1.640028",
        resolved_at=datetime.now(timezone.utc).isoformat(),
        expires_at=expires_at.isoformat(),
    )
    return source, item


async def create_feed_session(db: Database, *, source_id: int | None = None) -> dict:
    return await db.kids_feed_session_create(
        profile="noah",
        policy_version="test-v1",
        minimum_remaining_seconds=0,
        source_id=source_id,
    )


def load_app(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    return importlib.reload(importlib.import_module("app.main"))


def mock_upstream(monkeypatch, module, requests):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "i.ytimg.com":
            return httpx.Response(
                200,
                headers={"content-type": "image/jpeg"},
                content=b"jpeg",
                request=request,
            )
        return httpx.Response(
            200,
            headers={
                "accept-ranges": "bytes",
                "content-length": "5",
                "content-type": "video/mp4",
            },
            content=b"media",
            request=request,
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    monkeypatch.setattr(module, "_new_kids_http_client", lambda: client)


def test_feed_is_opaque_sanitary_and_profile_bound(tmp_path, monkeypatch):
    asyncio.run(seed_catalog(tmp_path / "sentinel.db"))
    module = load_app(tmp_path, monkeypatch)
    requests = []
    mock_upstream(monkeypatch, module, requests)

    with TestClient(module.app) as client:
        response = client.get("/v1/kids/feed", params={"limit": 1, "profile": "noah"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["state"] == "ready"
        assert isinstance(payload["catalog_revision"], str)
        assert set(payload["items"][0]) == {
            "id",
            "thumbnail_url",
            "duration_seconds",
            "visual_category",
        }
        assert "video_id" not in json.dumps(payload)
        assert "googlevideo" not in json.dumps(payload)
        assert payload["next_cursor"]
        assert client.get(payload["items"][0]["thumbnail_url"]).status_code == 200
        assert requests[-1].url.host == "i.ytimg.com"

        wrong_profile = client.get(
            "/v1/kids/feed",
            params={"cursor": payload["next_cursor"], "limit": 1, "profile": "other"},
        )
        assert wrong_profile.status_code == 409
        assert client.get("/v1/kids/feed", params={"cursor": "bad.cursor"}).status_code == 400


def test_new_feed_sessions_shuffle_the_balanced_catalog(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    asyncio.run(seed_catalog(db_path, qualities=(720,) * 8))
    module = load_app(tmp_path, monkeypatch)
    mock_upstream(monkeypatch, module, [])

    first_items = set()
    with TestClient(module.app) as client:
        for _ in range(12):
            assert client.get("/v1/kids/feed", params={"limit": 1}).status_code == 200
            with sqlite3.connect(db_path) as connection:
                first_items.add(
                    connection.execute(
                        """
                        SELECT f.item_id
                        FROM feed_sessions s
                        JOIN feed_session_items f ON f.feed_session_id=s.id
                        WHERE f.ordinal=0
                        ORDER BY s.created_at DESC
                        LIMIT 1
                        """
                    ).fetchone()[0]
                )

    assert len(first_items) > 1


def test_source_filtered_feed_session_contains_only_requested_source(tmp_path):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(720, 720)))
    source = asyncio.run(add_ready_source_item(db, suffix="filtered"))[0]
    session = asyncio.run(create_feed_session(db, source_id=source["id"]))

    with sqlite3.connect(db_path) as connection:
        source_ids = {
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT i.source_id
                FROM feed_session_items f
                JOIN catalog_items i ON i.id=f.item_id
                WHERE f.feed_session_id=?
                """,
                (session["id"],),
            )
        }
        stored_source_id = connection.execute(
            "SELECT source_id FROM feed_sessions WHERE id=?",
            (session["id"],),
        ).fetchone()[0]
    assert source_ids == {source["id"]}
    assert stored_source_id == source["id"]


def test_bound_session_omits_item_after_source_id_mutation(tmp_path):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(720,)))
    primary_source = asyncio.run(db.catalog_sources_list())[0]
    secondary_source, _item = asyncio.run(
        add_ready_source_item(db, suffix="page-bound")
    )
    session = asyncio.run(create_feed_session(db, source_id=primary_source["id"]))

    with sqlite3.connect(db_path) as connection:
        item_id = connection.execute(
            "SELECT item_id FROM feed_session_items WHERE feed_session_id=?",
            (session["id"],),
        ).fetchone()[0]
        connection.execute(
            """
            UPDATE catalog_items
            SET source_id=?, channel_id=?, channel_title=?
            WHERE id=?
            """,
            (
                secondary_source["id"],
                SECONDARY_CHANNEL_ID,
                secondary_source["title"],
                item_id,
            ),
        )
        connection.commit()

    page = asyncio.run(
        db.kids_feed_session_page(
            session["id"],
            profile="noah",
            offset=0,
            limit=60,
            policy_version="test-v1",
            minimum_remaining_seconds=0,
        )
    )
    assert page == {"status": "ok", "items": [], "next_offset": None}


def test_ineligible_source_yields_empty_feed_session(tmp_path):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path))
    source = asyncio.run(
        add_ready_source_item(db, suffix="unsafe", safety_verdict="UNSAFE")
    )[0]
    with pytest.raises(ValueError, match="Kids feed source is not eligible"):
        asyncio.run(create_feed_session(db, source_id=source["id"]))


def test_global_feed_session_contains_multiple_sources(tmp_path):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(720,)))
    primary_source = asyncio.run(db.catalog_sources_list())[0]
    secondary_source = asyncio.run(add_ready_source_item(db, suffix="global"))[0]
    session = asyncio.run(create_feed_session(db))

    with sqlite3.connect(db_path) as connection:
        source_ids = {
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT i.source_id
                FROM feed_session_items f
                JOIN catalog_items i ON i.id=f.item_id
                WHERE f.feed_session_id=?
                """,
                (session["id"],),
            )
        }
    assert {primary_source["id"], secondary_source["id"]} <= source_ids


def test_channels_are_sanitized_and_isolated_by_profile(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(720, 720)))
    secondary_source, secondary_item = asyncio.run(
        add_ready_source_item(db, suffix="felix")
    )
    asyncio.run(
        db.kids_source_profiles_set(
            secondary_source["id"],
            ["felix"],
            actor="test",
            reason="profile isolation",
            correlation_id="profile-isolation",
        )
    )
    asyncio.run(
        db.catalog_item_refresh(
            secondary_item["id"],
            title=secondary_item["title"],
            source_id=secondary_source["id"],
            thumbnail_url="https://i.ytimg.com/vi/secondary-felix/hqdefault.jpg",
            duration_seconds=42,
            visual_category="general",
            correlation_id="secondary-thumbnail",
            channel_id=SECONDARY_CHANNEL_ID,
            channel_title=secondary_source["title"],
        )
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE catalog_sources SET avatar_url=? WHERE id=?",
            ("https://evil.example/avatar.jpg", secondary_source["id"]),
        )
        connection.commit()
    module = load_app(tmp_path, monkeypatch)
    requests = []
    mock_upstream(monkeypatch, module, requests)

    with TestClient(module.app) as client:
        noah = client.get("/v1/kids/channels", params={"profile": "noah"})
        felix = client.get("/v1/kids/channels", params={"profile": "felix"})
        assert noah.status_code == 200
        assert felix.status_code == 200
        noah_channel = noah.json()["channels"]
        felix_channel = felix.json()["channels"]
        assert len(noah_channel) == 1
        assert felix_channel == []
        assert set(noah_channel[0]) == {
            "id",
            "poster_background_url",
            "avatar_url",
            "accessibility_label",
        }
        serialized = json.dumps(noah.json() | felix.json())
        assert CHANNEL_ID not in serialized
        assert SECONDARY_CHANNEL_ID not in serialized
        assert "youtube.com" not in serialized
        assert noah_channel[0]["poster_background_url"].startswith(
            "http://testserver/v1/kids/channels/"
        )
        assert "?profile=noah&v=" in noah_channel[0]["poster_background_url"]


def test_fresh_channel_responses_shuffle_the_eligible_order(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(720,)))
    asyncio.run(add_ready_source_item(db, suffix="shuffle"))
    module = load_app(tmp_path, monkeypatch)
    mock_upstream(monkeypatch, module, [])

    observed_orders = set()
    with TestClient(module.app) as client:
        for _ in range(16):
            response = client.get("/v1/kids/channels", params={"profile": "noah"})
            assert response.status_code == 200
            observed_orders.add(
                tuple(channel["id"] for channel in response.json()["channels"])
            )

    assert len(observed_orders) > 1


def test_channels_omit_missing_genuine_avatar_without_video_fallback(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(720,)))
    unsafe_source = asyncio.run(
        add_ready_source_item(db, suffix="unsafe", safety_verdict="UNSAFE")
    )[0]
    no_ready_source = asyncio.run(
        db.catalog_create(
            "source",
            {
                "kind": "channel",
                "reference": f"UC{'c' * 22}",
                "title": "No ready source",
                "correlation_id": "no-ready-source",
            },
        )
    )
    asyncio.run(
        db.catalog_source_safety_update(
            no_ready_source["id"],
            verdict="SAFE",
            reason="test",
            actor="test",
            correlation_id="no-ready-safety",
            policy_version="test-v1",
        )
    )
    asyncio.run(
        db.catalog_transition(
            "source",
            no_ready_source["id"],
            {
                "state": "approved",
                "actor": "test",
                "reason": "test",
                "correlation_id": "no-ready-approved",
            },
        )
    )
    no_ready_item = asyncio.run(
        db.catalog_create(
            "item",
            {
                "video_id": "no-ready-item",
                "title": "No ready item",
                "source_id": no_ready_source["id"],
                "channel_id": f"UC{'c' * 22}",
                "channel_title": "No ready source",
                "thumbnail_url": "https://i.ytimg.com/vi/no-ready/hqdefault.jpg",
                "correlation_id": "no-ready-item",
            },
        )
    )
    asyncio.run(
        db.catalog_transition(
            "item",
            no_ready_item["id"],
            {
                "state": "approved",
                "actor": "test",
                "reason": "test",
                "correlation_id": "no-ready-item-approved",
            },
        )
    )
    primary_source = asyncio.run(db.catalog_sources_list())[0]
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE catalog_sources SET avatar_url=? WHERE id=?",
            ("", primary_source["id"]),
        )
        connection.commit()
    module = load_app(tmp_path, monkeypatch)
    mock_upstream(monkeypatch, module, [])

    with TestClient(module.app) as client:
        payload = client.get("/v1/kids/channels", params={"profile": "noah"}).json()
        assert payload["state"] == "ready"
        assert payload["channels"] == []
    assert unsafe_source["id"] != primary_source["id"]


def test_playlist_is_absent_from_channel_wall_and_rejected_by_poster_management(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(720,)))
    playlist_source, playlist_item = asyncio.run(
        add_ready_source_item(
            db,
            suffix="playlist",
            kind="playlist",
            reference="PL-playlist-only",
        )
    )
    module = load_app(tmp_path, monkeypatch)
    mock_upstream(monkeypatch, module, [])

    with TestClient(module.app) as client:
        channels = client.get("/v1/kids/channels", params={"profile": "noah"})
        assert channels.status_code == 200
        assert all(
            channel["accessibility_label"] != playlist_source["title"]
            for channel in channels.json()["channels"]
        )

        poster = client.put(
            f"/api/kids/sources/{playlist_source['id']}/poster",
            json={
                "item_id": playlist_item["id"],
                "actor": "parent-test",
                "reason": "Playlist cannot have a channel poster",
                "correlation_id": "playlist-poster-rejected",
            },
        )
        assert poster.status_code == 409


def test_channel_artwork_proxy_rejects_declared_and_actual_oversized_responses(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "sentinel.db"
    asyncio.run(seed_catalog(db_path, qualities=(720,)))
    module = load_app(tmp_path, monkeypatch)
    mode = "declared"
    max_bytes = 8 * 1024 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "declared":
            return httpx.Response(
                200,
                headers={
                    "content-type": "image/jpeg",
                    "content-length": str(max_bytes + 1),
                },
                content=b"small",
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg", "content-length": "5"},
            content=b"x" * (max_bytes + 1),
            request=request,
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    monkeypatch.setattr(module, "_new_kids_http_client", lambda: client)

    with TestClient(module.app) as app_client:
        channel = app_client.get("/v1/kids/channels").json()["channels"][0]
        declared = app_client.get(channel["poster_background_url"])
        assert declared.status_code == 502

        mode = "actual"
        actual = app_client.get(channel["poster_background_url"])
        assert actual.status_code == 502


def test_channel_artwork_proxy_never_follows_redirects(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    asyncio.run(seed_catalog(db_path, qualities=(720,)))
    module = load_app(tmp_path, monkeypatch)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "i.ytimg.com":
            return httpx.Response(
                302,
                headers={"location": "https://example.invalid/artwork.jpg"},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=b"redirected",
            request=request,
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    monkeypatch.setattr(module, "_new_kids_http_client", lambda: client)

    with TestClient(module.app) as app_client:
        channel = app_client.get("/v1/kids/channels").json()["channels"][0]
        response = app_client.get(channel["poster_background_url"])

    assert response.status_code == 502
    assert len(requests) == 1
    assert requests[0].url.host == "i.ytimg.com"


def test_channel_poster_override_stays_on_source_and_falls_back_after_revoke(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(720, 720)))
    primary_source = asyncio.run(db.catalog_sources_list())[0]
    secondary_source, secondary_item = asyncio.run(
        add_ready_source_item(db, suffix="poster")
    )
    asyncio.run(
        db.catalog_item_refresh(
            secondary_item["id"],
            title=secondary_item["title"],
            source_id=secondary_source["id"],
            thumbnail_url="https://i.ytimg.com/vi/secondary-poster/hqdefault.jpg",
            duration_seconds=42,
            visual_category="general",
            correlation_id="secondary-poster-thumbnail",
            channel_id=SECONDARY_CHANNEL_ID,
            channel_title=secondary_source["title"],
        )
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE catalog_sources SET poster_item_id=? WHERE id=?",
            (secondary_item["id"], primary_source["id"]),
        )
        connection.commit()
    module = load_app(tmp_path, monkeypatch)
    requests = []
    mock_upstream(monkeypatch, module, requests)

    with TestClient(module.app) as client:
        payload = client.get("/v1/kids/channels").json()
        primary_channel = next(
            channel
            for channel in payload["channels"]
            if channel["accessibility_label"] == "Sentinel test source"
        )
        assert client.get(primary_channel["poster_background_url"]).status_code == 200
        assert "dataplane-0" in str(requests[-1].url)
        asyncio.run(
            db.catalog_transition(
                "item",
                1,
                {
                    "state": "revoked",
                    "actor": "test",
                    "reason": "poster revoked",
                    "correlation_id": "poster-revoked",
                },
            )
        )
        assert client.get(primary_channel["poster_background_url"]).status_code == 200
        assert "dataplane-1" in str(requests[-1].url)


def test_channel_feed_is_source_bound_across_pages_and_legacy_feed_remains(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(720, 720)))
    primary_source = asyncio.run(db.catalog_sources_list())[0]
    secondary_source, secondary_item = asyncio.run(
        add_ready_source_item(db, suffix="feed")
    )
    asyncio.run(
        db.catalog_item_refresh(
            secondary_item["id"],
            title=secondary_item["title"],
            source_id=secondary_source["id"],
            thumbnail_url="https://i.ytimg.com/vi/secondary-feed/hqdefault.jpg",
            duration_seconds=42,
            visual_category="general",
            correlation_id="secondary-feed-thumbnail",
            channel_id=SECONDARY_CHANNEL_ID,
            channel_title=secondary_source["title"],
        )
    )
    asyncio.run(
        db.catalog_item_safety_update(
            secondary_item["id"],
            verdict="SAFE",
            language="nl",
            content_kind="learning",
            age_suitability={"2": "SUITABLE", "6": "SUITABLE"},
            reason="secondary refreshed item safety",
            actor="test",
            correlation_id="secondary-feed-item-safety",
        )
    )
    module = load_app(tmp_path, monkeypatch)
    mock_upstream(monkeypatch, module, [])

    with TestClient(module.app) as client:
        channel_queries = 0
        original_eligible_channels = (
            module.app.state.runtime.db.kids_eligible_channels
        )

        async def counted_eligible_channels(*args, **kwargs):
            nonlocal channel_queries
            channel_queries += 1
            return await original_eligible_channels(*args, **kwargs)

        module.app.state.runtime.db.kids_eligible_channels = counted_eligible_channels
        channels = client.get("/v1/kids/channels").json()["channels"]
        primary_channel = next(
            channel
            for channel in channels
            if channel["accessibility_label"] == "Sentinel test source"
        )
        secondary_channel = next(
            channel
            for channel in channels
            if channel["accessibility_label"] == "Secondary feed"
        )
        first = client.get(
            "/v1/kids/feed",
            params={"profile": "noah", "channel": primary_channel["id"], "limit": 1},
        )
        assert first.status_code == 200
        first_payload = first.json()
        assert first_payload["next_cursor"]
        queries_after_first_page = channel_queries
        assert queries_after_first_page == 2
        second = client.get(
            "/v1/kids/feed",
            params={
                "profile": "noah",
                "cursor": first_payload["next_cursor"],
                "limit": 1,
            },
        )
        assert second.status_code == 200
        assert channel_queries == queries_after_first_page
        mismatch = client.get(
            "/v1/kids/feed",
            params={
                "profile": "noah",
                "channel": secondary_channel["id"],
                "cursor": first_payload["next_cursor"],
                "limit": 1,
            },
        )
        assert mismatch.status_code == 409
        assert client.get(
            "/v1/kids/feed",
            params={"channel": "not-a-channel"},
        ).status_code == 404
        assert client.get(
            "/v1/kids/feed",
            params={"profile": "felix", "channel": primary_channel["id"]},
        ).status_code == 404
        assert client.get("/v1/kids/feed", params={"limit": 1}).status_code == 200

        with sqlite3.connect(db_path) as connection:
            session_id = connection.execute(
                "SELECT id FROM feed_sessions WHERE source_id=?",
                (primary_source["id"],),
            ).fetchone()[0]
            bound_sources = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT i.source_id
                    FROM feed_session_items f
                    JOIN catalog_items i ON i.id=f.item_id
                    WHERE f.feed_session_id=?
                    """,
                    (session_id,),
                )
            }
        assert bound_sources == {primary_source["id"]}

        asyncio.run(
            db.catalog_transition(
                "source",
                primary_source["id"],
                {
                    "state": "revoked",
                    "actor": "test",
                    "reason": "channel revoked",
                    "correlation_id": "channel-revoked",
                },
            )
        )
        assert client.get(
            "/v1/kids/feed",
            params={"profile": "noah", "channel": primary_channel["id"]},
        ).status_code == 404


def test_feed_reuses_recent_policy_reconcile_until_forced(tmp_path, monkeypatch):
    asyncio.run(seed_catalog(tmp_path / "sentinel.db"))
    module = load_app(tmp_path, monkeypatch)
    mock_upstream(monkeypatch, module, [])
    calls = []

    async def fake_reconcile():
        calls.append(True)
        return 0

    with TestClient(module.app) as client:
        module.judge.reconcile_catalog_policy = fake_reconcile
        assert client.get("/v1/kids/feed", params={"limit": 1}).status_code == 200
        assert client.get("/v1/kids/feed", params={"limit": 1}).status_code == 200
        assert calls == []

        module.app.state.runtime.kids_reconciled_at = 0
        assert client.get("/v1/kids/feed", params={"limit": 1}).status_code == 200
        assert calls == [True]


def test_playback_relay_manifest_range_head_event_and_delete(tmp_path, monkeypatch):
    asyncio.run(seed_catalog(tmp_path / "sentinel.db", qualities=(1080,)))
    module = load_app(tmp_path, monkeypatch)
    requests = []
    mock_upstream(monkeypatch, module, requests)

    with TestClient(module.app) as client:
        item = client.get("/v1/kids/feed").json()["items"][0]
        selected = client.post(
            "/v1/kids/events",
            json={
                "asset_id": item["id"],
                "event": "selected",
                "profile": "noah",
            },
        )
        assert selected.status_code == 202
        created = client.post(
            "/v1/kids/playback-sessions",
            json={"asset_id": item["id"]},
        )
        assert created.status_code == 200
        lease = created.json()
        assert "video_id" not in lease
        manifest = client.get(lease["manifest_url"])
        assert manifest.status_code == 200
        manifest_payload = manifest.json()
        assert manifest_payload["transport"] == "adaptive_mpv"
        assert client.get(manifest_payload["status_url"]).status_code == 204
        other_asset = client.get("/v1/kids/feed").json()["items"][0]
        assert other_asset["id"] != item["id"]
        assert client.post(
            "/v1/kids/events",
            json={
                "asset_id": other_asset["id"],
                "event": "started",
                "session_id": lease["id"],
                "profile": "noah",
            },
        ).status_code == 409

        lease_checks = 0
        relay_lease_get = module.app.state.runtime.db.kids_relay_lease_get

        async def count_lease_checks(*args, **kwargs):
            nonlocal lease_checks
            lease_checks += 1
            return await relay_lease_get(*args, **kwargs)

        monkeypatch.setattr(
            module.app.state.runtime.db,
            "kids_relay_lease_get",
            count_lease_checks,
        )

        reconcile = module.app.state.runtime.reconcile_kids_catalog_policy

        async def reject_reconcile(*, force=False):
            raise AssertionError("media stream must not reconcile the catalog")

        monkeypatch.setattr(
            module.app.state.runtime,
            "reconcile_kids_catalog_policy",
            reject_reconcile,
        )
        media = client.get(
            manifest_payload["video_url"],
            headers={"Range": "bytes=0-4", "Cookie": "not-forwarded"},
        )
        assert media.status_code == 200
        assert media.content == b"media"
        assert lease_checks == 1
        upstream = requests[-1]
        assert upstream.headers["range"] == "bytes=0-4"
        assert upstream.headers["accept-encoding"] == "identity"
        assert "cookie" not in upstream.headers

        media_without_range = client.get(manifest_payload["video_url"])
        assert media_without_range.status_code == 200
        assert requests[-1].headers["range"] == "bytes=0-"
        assert lease_checks == 2

        head = client.head(manifest_payload["video_url"])
        assert head.status_code == 200
        assert head.content == b""
        assert "range" not in requests[-1].headers
        monkeypatch.setattr(
            module.app.state.runtime,
            "reconcile_kids_catalog_policy",
            reconcile,
        )

        event = client.post(
            "/v1/kids/events",
            json={
                "asset_id": item["id"],
                "event": "started",
                "session_id": lease["id"],
                "profile": "noah",
            },
        )
        assert event.status_code == 202
        assert client.get("/api/kids/watch-events").json()["events"][0]["video_id"] == "dataplane-0"

        assert client.delete(f"/v1/kids/playback-sessions/{lease['id']}").json() == {"closed": True}
        assert client.get(manifest_payload["status_url"]).status_code == 403
        assert client.post(
            "/v1/kids/events",
            json={
                "asset_id": item["id"],
                "event": "started",
                "profile": "noah",
            },
        ).status_code == 409
        assert client.post(
            "/v1/kids/events",
            json={
                "asset_id": item["id"],
                "event": "stopped",
                "session_id": lease["id"],
                "profile": "noah",
            },
        ).status_code == 202
        assert client.delete(f"/v1/kids/playback-sessions/{lease['id']}").json() == {"closed": False}


def test_playback_relay_follows_upstream_media_redirects(tmp_path, monkeypatch):
    asyncio.run(seed_catalog(tmp_path / "sentinel.db", qualities=(1080,)))
    module = load_app(tmp_path, monkeypatch)
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "rr1---sn.example.googlevideo.com":
            return httpx.Response(
                302,
                headers={"location": "https://media.test/video"},
                request=request,
            )
        return httpx.Response(
            206,
            headers={
                "accept-ranges": "bytes",
                "content-length": "5",
                "content-range": "bytes 0-4/5",
                "content-type": "video/mp4",
            },
            content=b"media",
            request=request,
        )

    upstream = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    monkeypatch.setattr(module, "_new_kids_http_client", lambda: upstream)

    with TestClient(module.app) as client:
        item = client.get("/v1/kids/feed").json()["items"][0]
        created = client.post(
            "/v1/kids/playback-sessions",
            json={"asset_id": item["id"]},
        ).json()
        response = client.get(
            f"/v1/kids/playback-sessions/{created['id']}/video",
            headers={"Range": "bytes=0-4"},
        )

    assert response.status_code == 206
    assert response.content == b"media"
    assert [request.url.host for request in requests[-2:]] == [
        "rr1---sn.example.googlevideo.com",
        "media.test",
    ]
    assert requests[-1].headers["range"] == "bytes=0-4"


def test_kill_switch_and_schedule_close_revoke_leases(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(1080,)))
    module = load_app(tmp_path, monkeypatch)
    mock_upstream(monkeypatch, module, [])

    with TestClient(module.app) as client:
        asset_id = client.get("/v1/kids/feed").json()["items"][0]["id"]
        lease_id = client.post(
            "/v1/kids/playback-sessions",
            json={"asset_id": asset_id},
        ).json()["id"]
        assert client.post(
            "/api/kids/control/kill-switch",
            json={
                "enabled": True,
                "actor": "test",
                "reason": "contract",
                "correlation_id": "dataplane-kill",
            },
        ).status_code == 200
        assert client.get(f"/v1/kids/playback-sessions/{lease_id}/status").status_code == 403

        assert client.post(
            "/api/kids/control/kill-switch",
            json={
                "enabled": False,
                "actor": "test",
                "reason": "ready",
                "correlation_id": "dataplane-ready",
            },
        ).status_code == 200
        second_asset_id = client.get("/v1/kids/feed").json()["items"][0]["id"]
        second_lease_id = client.post(
            "/v1/kids/playback-sessions",
            json={"asset_id": second_asset_id},
        ).json()["id"]
        schedule_id = client.get("/api/schedules").json()["rows"][0]["id"]
        assert client.post(
            f"/api/schedules/{schedule_id}/update",
            json={
                "name": "Closed",
                "enabled": False,
                "start": "00:00",
                "end": "00:00",
                "timezone": "UTC",
                "mode": "blocklist",
            },
        ).status_code == 200
        assert client.get(
            f"/v1/kids/playback-sessions/{second_lease_id}/status"
        ).status_code == 403

    with sqlite3.connect(db.db_path) as connection:
        state, reason = connection.execute(
            "SELECT state,revoked_reason FROM relay_leases WHERE id=?",
            (lease_id,),
        ).fetchone()
    assert state == "revoked"
    assert reason == "kill_switch"
    with sqlite3.connect(db.db_path) as connection:
        state, reason = connection.execute(
            "SELECT state,revoked_reason FROM relay_leases WHERE id=?",
            (second_lease_id,),
        ).fetchone()
    assert state == "revoked"
    assert reason == "schedule_closed"


def test_age_policy_removes_feed_item_and_revokes_active_playback(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(1080,)))
    module = load_app(tmp_path, monkeypatch)
    mock_upstream(monkeypatch, module, [])

    with TestClient(module.app) as client:
        asset_id = client.get(
            "/v1/kids/feed",
            params={"profile": "noah", "limit": 1},
        ).json()["items"][0]["id"]
        lease_id = client.post(
            "/v1/kids/playback-sessions",
            json={"asset_id": asset_id},
        ).json()["id"]
        source = asyncio.run(db.catalog_sources_list())[0]
        asyncio.run(
            db.catalog_source_safety_update(
                source["id"],
                verdict="SAFE",
                language="nl",
                content_kind="learning",
                age_suitability={"2": "SUITABLE", "6": "UNSUITABLE"},
                reason="no longer suitable for age six",
                actor="test",
                correlation_id="age-policy-change",
            )
        )

        assert client.get(
            "/v1/kids/feed",
            params={"profile": "noah", "limit": 1},
        ).json()["items"] == []
        assert client.get(
            f"/v1/kids/playback-sessions/{lease_id}/status"
        ).status_code == 403

    with sqlite3.connect(db_path) as connection:
        state, reason = connection.execute(
            "SELECT state,revoked_reason FROM relay_leases WHERE id=?",
            (lease_id,),
        ).fetchone()
    assert state == "revoked"
    assert reason == "profile_unassigned"


def test_lease_recheck_uses_current_quality_policy(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(720,)))
    module = load_app(tmp_path, monkeypatch)
    mock_upstream(monkeypatch, module, [])

    with TestClient(module.app) as client:
        asset_id = client.get("/v1/kids/feed").json()["items"][0]["id"]
        lease_id = client.post(
            "/v1/kids/playback-sessions",
            json={"asset_id": asset_id},
        ).json()["id"]

        assert asyncio.run(
            db.kids_relay_lease_get(
                lease_id,
                minimum_quality_height=1080,
            )
        ) is None

    with sqlite3.connect(db.db_path) as connection:
        state, reason = connection.execute(
            "SELECT state,revoked_reason FROM relay_leases WHERE id=?",
            (lease_id,),
        ).fetchone()
    assert state == "revoked"
    assert reason == "catalog_ineligible"


def test_catalog_addition_keeps_cursor_and_current_authorized_asset_valid(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(720, 1080)))
    module = load_app(tmp_path, monkeypatch)
    mock_upstream(monkeypatch, module, [])

    with TestClient(module.app) as client:
        first = client.get("/v1/kids/feed", params={"limit": 1}).json()
        source = asyncio.run(db.catalog_sources_list())
        asyncio.run(
            db.catalog_create(
                "item",
                {
                    "video_id": "dataplane-new",
                    "title": "New item",
                    "source_id": source[0]["id"],
                    "channel_id": CHANNEL_ID,
                    "channel_title": "Sentinel test source",
                    "correlation_id": "dataplane-new-item",
                },
            )
        )
        continued = client.get(
            "/v1/kids/feed",
            params={"cursor": first["next_cursor"], "limit": 1},
        )
        assert continued.status_code == 200
        assert client.get(first["items"][0]["thumbnail_url"]).status_code == 200
        assert client.post(
            "/v1/kids/playback-sessions",
            json={"asset_id": first["items"][0]["id"]},
        ).status_code == 200
        assert client.post(
            "/v1/kids/playback-sessions",
            json={"asset_id": "unknown-asset"},
        ).status_code == 404


def test_feed_page_skips_invalid_ordinal_and_keeps_next_cursor(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(seed_catalog(db_path, qualities=(720, 720, 720, 720)))
    module = load_app(tmp_path, monkeypatch)
    mock_upstream(monkeypatch, module, [])

    with TestClient(module.app) as client:
        first = client.get("/v1/kids/feed", params={"limit": 1}).json()
        assert first["next_cursor"]

        with sqlite3.connect(db.db_path) as connection:
            session_id, item_id = connection.execute(
                """
                SELECT feed_session_id,item_id
                FROM feed_session_items
                WHERE ordinal=1
                """
            ).fetchone()
            connection.execute(
                "UPDATE catalog_items SET channel_id=? WHERE id=?",
                ("wrong-channel", item_id),
            )
            connection.commit()

        page = client.get(
            "/v1/kids/feed",
            params={
                "cursor": first["next_cursor"],
                "limit": 1,
                "profile": "noah",
            },
        )
        assert page.status_code == 200
        payload = page.json()
        assert len(payload["items"]) == 1
        assert payload["next_cursor"]

        with sqlite3.connect(db.db_path) as connection:
            assert connection.execute(
                """
                SELECT COUNT(*)
                FROM feed_session_items
                WHERE feed_session_id=?
                """,
                (session_id,),
            ).fetchone()[0] == 4
