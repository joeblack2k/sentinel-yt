import asyncio
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from app.db import Database
from app.kids_api import _select_kids_shelves
from app.services.kids_database import KIDS_SHELF_NAMES


def _item(
    item_id: int,
    source_id: int,
    *,
    language: str,
    content_kind: str,
    history_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> dict:
    return {
        "id": item_id,
        "source_id": source_id,
        "_source_language": language,
        "_source_content_kind": content_kind,
        "_profile_history_at": history_at.isoformat() if history_at else None,
        "_profile_completed_at": completed_at.isoformat() if completed_at else None,
    }


def test_shelf_languages_kinds_and_source_diversity():
    items = []
    for source_id in range(1, 5):
        items.append(
            _item(
                source_id,
                source_id,
                language="nl",
                content_kind="learning",
            )
        )
    for source_id in range(5, 9):
        items.append(
            _item(
                source_id,
                source_id,
                language="mixed",
                content_kind="learning",
            )
        )
    for source_id in range(9, 13):
        items.append(
            _item(
                source_id,
                source_id,
                language="en",
                content_kind="entertainment",
            )
        )
    for source_id in range(13, 17):
        items.append(
            _item(
                source_id,
                source_id,
                language="mixed",
                content_kind="entertainment",
            )
        )
    items.extend(
        [
            _item(17, 17, language="en", content_kind="learning"),
            _item(18, 18, language="nl", content_kind="entertainment"),
        ]
    )

    noah = _select_kids_shelves(
        items,
        profile="noah",
        day="2026-09-04",
        now=datetime(2026, 9, 4, tzinfo=timezone.utc),
    )
    felix = _select_kids_shelves(
        items,
        profile="felix",
        day="2026-09-04",
        now=datetime(2026, 9, 4, tzinfo=timezone.utc),
    )

    assert len(noah["learning"]) == 7
    assert len(noah["fun"]) == 7
    assert len(felix["learning"]) == 5
    assert len(felix["fun"]) == 5
    assert [item["_source_language"] for item in noah["learning"]] == [
        "nl"
    ] * 4 + ["mixed"] * 3
    assert [item["_source_language"] for item in noah["fun"]] == [
        "en"
    ] * 4 + ["mixed"] * 3
    assert [item["_source_language"] for item in felix["learning"]] == [
        "nl"
    ] * 4 + ["mixed"]
    assert [item["_source_language"] for item in felix["fun"]] == [
        "nl"
    ] + ["mixed"] * 4
    assert all(
        item["_source_language"] in {"nl", "mixed"}
        and item["_source_content_kind"] in {"learning", "mixed"}
        for item in noah["learning"] + felix["learning"]
    )
    assert all(
        item["_source_language"] in {"en", "mixed"}
        and item["_source_content_kind"] in {"entertainment", "mixed"}
        for item in noah["fun"]
    )
    assert all(
        item["_source_language"] in {"nl", "mixed"}
        and item["_source_content_kind"] in {"entertainment", "mixed"}
        for item in felix["fun"]
    )
    assert 17 not in {item["id"] for item in noah["learning"]}
    assert 18 not in {item["id"] for item in noah["fun"]}

    for shelves in (noah, felix):
        for selected in shelves.values():
            assert len({item["source_id"] for item in selected}) == len(selected)
        assert max(
            Counter(item["source_id"] for selected in shelves.values() for item in selected).values(),
            default=0,
        ) <= 2


def test_shelf_history_cooldown_started_completed_and_daily_determinism():
    now = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
    items = [
        _item(
            1,
            1,
            language="nl",
            content_kind="learning",
            history_at=now - timedelta(hours=1),
        ),
        _item(
            2,
            2,
            language="nl",
            content_kind="learning",
            history_at=now - timedelta(days=1),
            completed_at=now - timedelta(days=1),
        ),
        _item(
            3,
            3,
            language="nl",
            content_kind="learning",
            history_at=now - timedelta(days=8),
            completed_at=now - timedelta(days=8),
        ),
        _item(4, 4, language="nl", content_kind="learning"),
    ]

    first = _select_kids_shelves(
        items,
        profile="noah",
        day="2026-09-04",
        now=now,
    )
    repeat = _select_kids_shelves(
        items,
        profile="noah",
        day="2026-09-04",
        now=now,
    )
    next_day = _select_kids_shelves(
        items,
        profile="noah",
        day="2026-09-05",
        now=now + timedelta(days=1),
    )

    assert {
        item["id"] for item in first["again"]
    } == {1, 2, 3}
    assert 2 not in {item["id"] for item in first["learning"]}
    assert 3 in {item["id"] for item in first["learning"]}
    assert 4 not in {item["id"] for item in first["again"]}
    assert {
        shelf: [item["id"] for item in selected]
        for shelf, selected in first.items()
    } == {
        shelf: [item["id"] for item in selected]
        for shelf, selected in repeat.items()
    }
    assert {
        shelf: [item["id"] for item in selected]
        for shelf, selected in first.items()
    } != {
        shelf: [item["id"] for item in selected]
        for shelf, selected in next_day.items()
    }


async def _ready_item(
    db: Database,
    index: int,
    *,
    language: str,
    content_kind: str,
    profile_slugs: list[str],
) -> tuple[dict, dict]:
    reference = f"UC{index:022d}"
    source = await db.catalog_create(
        "source",
        {
            "kind": "channel",
            "reference": reference,
            "title": f"Shelf source {index}",
            "language": language,
            "content_kind": content_kind,
            "profile_slugs": profile_slugs,
            "correlation_id": f"shelf-source-{index}",
        },
    )
    await db.catalog_source_safety_update(
        source["id"],
        verdict="SAFE",
        language=language,
        content_kind=content_kind,
        reason="shelf test",
        actor="test",
        correlation_id=f"shelf-safety-{index}",
        policy_version="test-v1",
    )
    source = await db.catalog_transition(
        "source",
        source["id"],
        {
            "state": "approved",
            "actor": "test",
            "reason": "shelf test",
            "correlation_id": f"shelf-source-approved-{index}",
        },
    )
    video_id = f"shelf-{index:06d}"
    item = await db.catalog_create(
        "item",
        {
            "video_id": video_id,
            "title": f"Shelf item {index}",
            "source_id": source["id"],
            "channel_id": reference,
            "channel_title": source["title"],
            "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "duration_seconds": 60 + index,
            "visual_category": "educational" if content_kind == "learning" else "play",
            "correlation_id": f"shelf-item-{index}",
        },
    )
    item = await db.catalog_transition(
        "item",
        item["id"],
        {
            "state": "approved",
            "actor": "test",
            "reason": "shelf test",
            "correlation_id": f"shelf-item-approved-{index}",
        },
    )
    expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
    await db.kids_resolve_success(
        item_id=item["id"],
        candidate={
            "kind": "adaptive_mpv",
            "media_url": (
                f"https://rr1---sn.example.googlevideo.com/video/{video_id}"
                f"?expire={int(expires_at.timestamp())}&sig=test"
            ),
            "audio_url": (
                f"https://rr1---sn.example.googlevideo.com/audio/{video_id}"
                f"?expire={int(expires_at.timestamp())}&sig=test"
            ),
            "quality_height": 720,
            "codec": "avc1.640028",
        },
        quality_height=720,
        codec="avc1.640028",
        resolved_at=datetime.now(timezone.utc).isoformat(),
        expires_at=expires_at.isoformat(),
    )
    return source, item


async def _seed_shelf_catalog(
    db_path,
    specs: list[tuple[int, str, str, list[str]]],
) -> Database:
    db = Database(str(db_path))
    await db.init()
    await db.set_setting("kids_kill_switch", "false")
    await db.set_setting("timezone", "UTC")
    schedule = (await db.list_schedules())[0]
    await db.update_schedule(
        int(schedule["id"]),
        name="Shelf tests",
        enabled=True,
        start="00:00",
        end="00:00",
        timezone="UTC",
        mode="blocklist",
    )
    for index, language, content_kind, profiles in specs:
        await _ready_item(
            db,
            index,
            language=language,
            content_kind=content_kind,
            profile_slugs=profiles,
        )
    return db


def _load_app(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    return importlib.reload(importlib.import_module("app.main"))


def _mock_upstream(monkeypatch, module, requests):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg", "content-length": "4"},
            content=b"jpeg",
            request=request,
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    monkeypatch.setattr(module, "_new_kids_http_client", lambda: client)


def _asset_item_ids(db_path, payload):
    assets = [
        (shelf["id"], item["id"])
        for shelf in payload["shelves"]
        for item in shelf["items"]
    ]
    if not assets:
        return {shelf["id"]: [] for shelf in payload["shelves"]}
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT asset_id,item_id FROM feed_session_items WHERE asset_id IN ({})".format(
                ",".join("?" for _ in assets)
            ),
            [asset_id for _shelf_id, asset_id in assets],
        ).fetchall()
    item_by_asset = {asset_id: item_id for asset_id, item_id in rows}
    return {
        shelf["id"]: [
            item_by_asset[item["id"]]
            for item in shelf["items"]
            if item["id"] in item_by_asset
        ]
        for shelf in payload["shelves"]
    }


def _daily_rows(db_path, day: str, profile: str):
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT shelf,ordinal,item_id
            FROM kids_daily_library
            WHERE day=? AND profile=?
            ORDER BY shelf,ordinal
            """,
            (day, profile),
        ).fetchall()
    return rows


def test_shelves_api_is_array_opaque_stable_and_profile_sized(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    specs = [
        *[
            (index, "nl", "learning", ["noah", "felix"])
            for index in range(1, 8)
        ],
        *[
            (index, "en", "entertainment", ["noah", "felix"])
            for index in range(101, 109)
        ],
        *[
            (index, "nl", "entertainment", ["noah", "felix"])
            for index in range(201, 206)
        ],
    ]
    db = asyncio.run(_seed_shelf_catalog(db_path, specs))
    history_item = asyncio.run(db.catalog_items_list())[0]
    asyncio.run(
        db.kids_watch_event_record(
            video_id=history_item["video_id"],
            event="started",
            profile="noah",
            position_seconds=1,
            session_id="",
            startup_ms=None,
            correlation_id="shelf-overlap-history",
        )
    )
    module = _load_app(tmp_path, monkeypatch)
    requests = []
    _mock_upstream(monkeypatch, module, requests)

    with TestClient(module.app) as client:
        first_response = client.get("/v1/kids/shelves", params={"profile": "noah"})
        assert first_response.status_code == 200
        first = first_response.json()
        assert set(first) == {
            "state",
            "catalog_revision",
            "retry_after_seconds",
            "shelves",
        }
        assert first["state"] == "ready"
        assert isinstance(first["shelves"], list)
        assert [shelf["id"] for shelf in first["shelves"]] == list(KIDS_SHELF_NAMES)
        assert [shelf["icon"] for shelf in first["shelves"]] == [
            "book.fill",
            "star.fill",
            "arrow.counterclockwise",
        ]
        assert all(set(shelf) == {"id", "icon", "items"} for shelf in first["shelves"])
        assert all(
            set(item) == {
                "id",
                "thumbnail_url",
                "duration_seconds",
                "visual_category",
            }
            for shelf in first["shelves"]
            for item in shelf["items"]
        )
        assert len(first["shelves"][0]["items"]) == 7
        assert len(first["shelves"][1]["items"]) == 7
        assert len(first["shelves"][2]["items"]) == 1
        serialized = json.dumps(first)
        assert "video_id" not in serialized
        assert "googlevideo" not in serialized
        assert "youtube.com" not in serialized
        assert client.get(first["shelves"][0]["items"][0]["thumbnail_url"]).status_code == 200
        assert requests[-1].url.host == "i.ytimg.com"
        assert client.post(
            "/v1/kids/playback-sessions",
            json={"asset_id": first["shelves"][0]["items"][0]["id"]},
        ).status_code == 200
        learning_assets = {
            item["id"] for item in first["shelves"][0]["items"]
        }
        again_assets = {
            item["id"] for item in first["shelves"][2]["items"]
        }
        assert learning_assets & again_assets

        first_ids = _asset_item_ids(db_path, first)
        day = datetime.now(timezone.utc).date().isoformat()
        initial_rows = _daily_rows(db_path, day, "noah")
        assert len(initial_rows) == 21
        assert [row[2] for row in initial_rows if row[0] == "learning"][:7] == first_ids["learning"]
        assert [row[2] for row in initial_rows if row[0] == "fun"][:7] == first_ids["fun"]

        _, new_item = asyncio.run(
            _ready_item(
                db,
                999,
                language="nl",
                content_kind="learning",
                profile_slugs=["noah"],
            )
        )
        second_response = client.get("/v1/kids/shelves", params={"profile": "noah"})
        assert second_response.status_code == 200
        second_ids = _asset_item_ids(db_path, second_response.json())
        assert second_ids == first_ids
        assert new_item["id"] not in {
            item_id
            for shelf_ids in second_ids.values()
            for item_id in shelf_ids
        }

        felix_response = client.get("/v1/kids/shelves", params={"profile": "felix"})
        assert felix_response.status_code == 200
        felix = felix_response.json()
        assert isinstance(felix["shelves"], list)
        assert len(felix["shelves"][0]["items"]) == 5
        assert len(felix["shelves"][1]["items"]) == 5
        assert felix["shelves"][2]["items"] == []


def test_again_history_is_profile_bound_and_accepts_started(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(
        _seed_shelf_catalog(
            db_path,
            [(1, "nl", "learning", ["noah", "felix"])],
        )
    )
    item = asyncio.run(db.catalog_items_list())[0]
    asyncio.run(
        db.kids_watch_event_record(
            video_id=item["video_id"],
            event="started",
            profile="noah",
            position_seconds=2,
            session_id="",
            startup_ms=None,
            correlation_id="shelf-started-history",
        )
    )
    module = _load_app(tmp_path, monkeypatch)
    _mock_upstream(monkeypatch, module, [])

    with TestClient(module.app) as client:
        noah = client.get("/v1/kids/shelves", params={"profile": "noah"})
        felix = client.get("/v1/kids/shelves", params={"profile": "felix"})
        assert noah.status_code == 200
        assert felix.status_code == 200
        assert _asset_item_ids(db_path, noah.json())["again"] == [item["id"]]
        assert _asset_item_ids(db_path, felix.json())["again"] == []


def test_shelves_keep_materialized_gaps_after_revoke_and_block(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    specs = [
        (index, "nl", "learning", ["noah"])
        for index in range(1, 9)
    ]
    db = asyncio.run(_seed_shelf_catalog(db_path, specs))
    module = _load_app(tmp_path, monkeypatch)
    _mock_upstream(monkeypatch, module, [])

    with TestClient(module.app) as client:
        first = client.get("/v1/kids/shelves", params={"profile": "noah"}).json()
        first_ids = _asset_item_ids(db_path, first)["learning"]
        assert len(first_ids) == 7
        revoked_id, blocked_id = first_ids[:2]
        with sqlite3.connect(db_path) as connection:
            source_ids = dict(
                connection.execute(
                    f"""
                    SELECT id,source_id
                    FROM catalog_items
                    WHERE id IN ({",".join("?" for _ in first_ids[:2])})
                    """,
                    first_ids[:2],
                ).fetchall()
            )

        lease = client.post(
            "/v1/kids/playback-sessions",
            json={"asset_id": first["shelves"][0]["items"][0]["id"]},
        )
        assert lease.status_code == 200
        lease_id = lease.json()["id"]
        asyncio.run(
            db.catalog_transition(
                "source",
                source_ids[revoked_id],
                {
                    "state": "revoked",
                    "actor": "parent",
                    "reason": "revoke shelf test",
                    "correlation_id": "shelf-revoke",
                },
            )
        )
        asyncio.run(
            db.catalog_transition(
                "source",
                source_ids[blocked_id],
                {
                    "state": "blocked",
                    "actor": "parent",
                    "reason": "block shelf test",
                    "correlation_id": "shelf-block",
                },
            )
        )
        _new_source, new_item = asyncio.run(
            _ready_item(
                db,
                999,
                language="nl",
                content_kind="learning",
                profile_slugs=["noah"],
            )
        )

        current = client.get("/v1/kids/shelves", params={"profile": "noah"})
        assert current.status_code == 200
        current_ids = _asset_item_ids(db_path, current.json())["learning"]
        assert current_ids == [
            item_id for item_id in first_ids if item_id not in {revoked_id, blocked_id}
        ]
        assert new_item["id"] not in current_ids
        rows = _daily_rows(
            db_path,
            datetime.now(timezone.utc).date().isoformat(),
            "noah",
        )
        assert [row[2] for row in rows if row[0] == "learning"][:7] == first_ids
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                "SELECT state FROM relay_leases WHERE id=?",
                (lease_id,),
            ).fetchone()[0] == "revoked"
        assert client.post(
            "/v1/kids/playback-sessions",
            json={"asset_id": first["shelves"][0]["items"][0]["id"]},
        ).status_code in {403, 409}
