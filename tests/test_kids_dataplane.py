import asyncio
import importlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from app.db import Database


CHANNEL_ID = f"UC{'a' * 22}"


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

        media = client.get(
            manifest_payload["video_url"],
            headers={"Range": "bytes=0-4", "Cookie": "not-forwarded"},
        )
        assert media.status_code == 200
        assert media.content == b"media"
        upstream = requests[-1]
        assert upstream.headers["range"] == "bytes=0-4"
        assert upstream.headers["accept-encoding"] == "identity"
        assert "cookie" not in upstream.headers

        head = client.head(manifest_payload["video_url"])
        assert head.status_code == 200
        assert head.content == b""

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


def test_revision_change_invalidates_cursor_and_unknown_asset_has_no_fallback(
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
        stale = client.get(
            "/v1/kids/feed",
            params={"cursor": first["next_cursor"], "limit": 1},
        )
        assert stale.status_code == 409
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
