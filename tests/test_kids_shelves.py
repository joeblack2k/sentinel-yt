import asyncio
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from app.db import Database
from app.kids_api import KIDS_PROFILE_SHELF_IDS, KIDS_SHELF_PAGE_SIZE, _encode_kids_cursor, _select_kids_shelves


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
            _item(19, 19, language="unknown", content_kind="unknown"),
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
    next_day = _select_kids_shelves(
        items,
        profile="noah",
        day="2026-09-05",
        now=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )

    assert set(noah) == {"new", "learning-nl", "fun-en", "fun-nl"}
    assert set(felix) == {"new", "learning-nl", "fun-nl"}
    assert all(
        item["_source_language"] in {"nl", "mixed"}
        and item["_source_content_kind"] in {"learning", "mixed"}
        for item in noah["learning-nl"] + felix["learning-nl"]
    )
    assert all(
        item["_source_language"] in {"en", "mixed"}
        and item["_source_content_kind"] in {"entertainment", "mixed"}
        for item in noah["fun-en"]
    )
    assert all(
        item["_source_language"] in {"nl", "mixed"}
        and item["_source_content_kind"] in {"entertainment", "mixed"}
        for item in noah["fun-nl"] + felix["fun-nl"]
    )
    assert all(selected for selected in noah.values())
    assert all(selected for selected in felix.values())
    assert 19 not in {
        item["id"]
        for shelves in (noah, felix)
        for selected in shelves.values()
        for item in selected
    }
    assert {
        item["id"] for selected in next_day.values() for item in selected
    } == {
        item["id"] for selected in noah.values() for item in selected
    }
    assert [item["id"] for item in next_day["learning-nl"]] != [
        item["id"] for item in noah["learning-nl"]
    ]

    for shelves in (noah, felix):
        selected_ids = [
            item["id"] for selected in shelves.values() for item in selected
        ]
        assert len(selected_ids) == len(set(selected_ids))
        assert max(Counter(
            item["source_id"] for selected in shelves.values() for item in selected
        ).values(), default=0) == 1


def test_shelf_completed_only_history_and_daily_determinism():
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
    assert {
        item["id"] for item in first["again"]
    } == {2, 3}
    assert 1 in {
        item["id"] for shelf, values in first.items()
        if shelf != "again" for item in values
    }
    assert 2 not in {item["id"] for item in first["learning-nl"]}
    assert 3 not in {item["id"] for item in first["learning-nl"]}
    assert 4 in {item["id"] for item in first["learning-nl"]}
    assert 4 not in {item["id"] for item in first["again"]}
    assert {
        shelf: [item["id"] for item in selected]
        for shelf, selected in first.items()
    } == {
        shelf: [item["id"] for item in selected]
        for shelf, selected in repeat.items()
    }


def test_single_old_completion_remains_exclusively_in_again():
    now = datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
    items = [_item(
        1, 1, language="nl", content_kind="learning",
        history_at=now - timedelta(days=30),
        completed_at=now - timedelta(days=30),
    )]
    shelves = _select_kids_shelves(items, profile="noah", day="2026-09-04", now=now)
    assert [item["id"] for item in shelves["again"]] == [1]
    assert all(not items for shelf, items in shelves.items() if shelf != "again")


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
        age_suitability={
            "2": "SUITABLE" if "felix" in profile_slugs else "UNSUITABLE",
            "6": "SUITABLE" if "noah" in profile_slugs else "UNSUITABLE",
        },
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
    item = await db.catalog_item_safety_update(
        item["id"],
        verdict="SAFE",
        language=language,
        content_kind=content_kind,
        age_suitability={
            "2": "SUITABLE" if "felix" in profile_slugs else "UNSUITABLE",
            "6": "SUITABLE" if "noah" in profile_slugs else "UNSUITABLE",
        },
        reason="shelf item safety",
        actor="test",
        correlation_id=f"shelf-item-safety-{index}",
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


def _item_ids_for_assets(db_path, asset_ids):
    if not asset_ids:
        return []
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT asset_id,item_id FROM feed_session_items WHERE asset_id IN ({})".format(
                ",".join("?" for _ in asset_ids)
            ),
            asset_ids,
        ).fetchall()
    item_by_asset = dict(rows)
    return [item_by_asset[asset_id] for asset_id in asset_ids]


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
            for index in range(1, 16)
        ],
        *[
            (index, "en", "entertainment", ["noah"])
            for index in range(101, 116)
        ],
        *[
            (index, "nl", "entertainment", ["noah", "felix"])
            for index in range(201, 216)
        ],
        *[
            (index, "nl", "unknown", ["noah", "felix"])
            for index in range(301, 316)
        ],
    ]
    db = asyncio.run(_seed_shelf_catalog(db_path, specs))
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
        assert [shelf["id"] for shelf in first["shelves"]] == list(
            KIDS_PROFILE_SHELF_IDS["noah"][:-1]
        )
        assert [shelf["icon"] for shelf in first["shelves"]] == [
            "sparkles",
            "book.fill",
            "globe",
            "star.fill",
        ]
        assert all(
            set(shelf) == {"id", "icon", "items", "next_cursor"}
            for shelf in first["shelves"]
        )
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
        assert all(
            len(shelf["items"]) == KIDS_SHELF_PAGE_SIZE
            and shelf["next_cursor"]
            for shelf in first["shelves"]
        )
        first_assets = {
            item["id"]
            for shelf in first["shelves"]
            for item in shelf["items"]
        }
        assert len(first_assets) == 4 * KIDS_SHELF_PAGE_SIZE
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
        original_tails = {}
        for shelf in first["shelves"]:
            next_page = client.get(
                "/v1/kids/feed",
                params={"cursor": shelf["next_cursor"], "limit": KIDS_SHELF_PAGE_SIZE},
            )
            assert next_page.status_code == 200
            assert len(next_page.json()["items"]) == 3
            assert next_page.json()["next_cursor"] is None
            original_tails[shelf["id"]] = next_page.json()["items"]
            assert first_assets.isdisjoint(
                item["id"] for item in next_page.json()["items"]
            )

        day = datetime.now(timezone.utc).date().isoformat()
        initial_rows = _daily_rows(db_path, day, "noah")
        assert len(initial_rows) == 4 * 72

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
        current_rows = _daily_rows(db_path, day, "noah")
        occupied = {row for row in initial_rows if row[2] is not None}
        assert occupied.issubset(current_rows)
        for shelf in first["shelves"]:
            old_tail = client.get("/v1/kids/feed", params={"cursor": shelf["next_cursor"], "limit": KIDS_SHELF_PAGE_SIZE})
            assert old_tail.status_code == 200
            assert old_tail.json()["items"] == original_tails[shelf["id"]]
        tail_item_ids = [item_id for ids in second_ids.values() for item_id in ids]
        for shelf in second_response.json()["shelves"]:
            if shelf["next_cursor"]:
                tail = client.get(
                    "/v1/kids/feed",
                    params={
                        "cursor": shelf["next_cursor"],
                        "limit": KIDS_SHELF_PAGE_SIZE,
                    },
                ).json()
                tail_item_ids.extend(
                    _item_ids_for_assets(
                        db_path,
                        [item["id"] for item in tail["items"]],
                    )
                )
        assert new_item["id"] in tail_item_ids

        felix_response = client.get("/v1/kids/shelves", params={"profile": "felix"})
        assert felix_response.status_code == 200
        felix = felix_response.json()
        assert [shelf["id"] for shelf in felix["shelves"]] == list(
            KIDS_PROFILE_SHELF_IDS["felix"][:-1]
        )
        assert all(len(shelf["items"]) == KIDS_SHELF_PAGE_SIZE for shelf in felix["shelves"])


def test_new_session_interleaves_sources_in_existing_daily_slots(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(_seed_shelf_catalog(db_path, [
        (index, "en", "entertainment", ["noah"]) for index in range(1, 19)
    ]))
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE catalog_items SET source_id=1, "
            "channel_id=(SELECT reference FROM catalog_sources WHERE id=1), "
            "channel_title=(SELECT title FROM catalog_sources WHERE id=1) WHERE id<=16"
        )
    for item_id in range(2, 17):
        asyncio.run(db.catalog_item_safety_update(
            item_id, verdict="SAFE", language="en", content_kind="entertainment",
            age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
            reason="Reassess changed fixture metadata", actor="test",
            correlation_id=f"regroup-{item_id}", sync_backlog=False,
        ))
    day = datetime.now(timezone.utc).date().isoformat()
    asyncio.run(db.kids_daily_library_get_or_create(
        day=day, profile="noah", shelf_limit=72,
        proposed_item_ids={"fun-en": list(range(1, 19))},
    ))
    occupied = {row for row in _daily_rows(db_path, day, "noah") if row[2] is not None}
    module = _load_app(tmp_path, monkeypatch)
    _mock_upstream(monkeypatch, module, [])
    with TestClient(module.app) as client:
        response = client.get("/v1/kids/shelves", params={"profile": "noah"})
        assert response.status_code == 200
        shelf = next(s for s in response.json()["shelves"] if s["id"] == "fun-en")
        ids = _asset_item_ids(db_path, response.json())["fun-en"]
        assert 17 in ids[:3] and 18 in ids[:3]
        assert shelf["next_cursor"] is not None
        tail = client.get("/v1/kids/feed", params={"cursor": shelf["next_cursor"]})
        assert tail.status_code == 200
        ids += _item_ids_for_assets(db_path, [item["id"] for item in tail.json()["items"]])
        assert len(ids) == len(set(ids)) == 18
        assert set(ids) == set(range(1, 19))
        assert occupied.issubset(_daily_rows(db_path, day, "noah"))


def test_again_history_is_profile_bound_and_requires_completion(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(
        _seed_shelf_catalog(
            db_path,
            [
                (index, "nl", "learning", ["noah", "felix"])
                for index in range(1, 4)
            ],
        )
    )
    items = asyncio.run(db.catalog_items_list())
    for item in items:
        asyncio.run(
            db.kids_watch_event_record(
                video_id=item["video_id"],
                event="completed",
                profile="noah",
                position_seconds=2,
                session_id="",
                startup_ms=None,
                correlation_id=f"shelf-started-history-{item['id']}",
            )
        )
    module = _load_app(tmp_path, monkeypatch)
    _mock_upstream(monkeypatch, module, [])

    with TestClient(module.app) as client:
        noah = client.get("/v1/kids/shelves", params={"profile": "noah"})
        felix = client.get("/v1/kids/shelves", params={"profile": "felix"})
        assert noah.status_code == 200
        assert felix.status_code == 200
        assert set(_asset_item_ids(db_path, noah.json())["again"]) == {
            item["id"] for item in items
        }
        assert "again" not in _asset_item_ids(db_path, felix.json())


def test_completion_moves_existing_daily_owner_to_again(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(_seed_shelf_catalog(
        db_path, [(i, "nl", "learning", ["noah", "felix"]) for i in range(1, 4)],
    ))
    module = _load_app(tmp_path, monkeypatch)
    _mock_upstream(monkeypatch, module, [])
    with TestClient(module.app) as client:
        before = client.get("/v1/kids/shelves", params={"profile": "noah"}).json()
        before_ids = _asset_item_ids(db_path, before)
        assert "again" not in before_ids
        item = asyncio.run(db.catalog_items_list())[0]
        assert item["id"] in {i for ids in before_ids.values() for i in ids}
        with sqlite3.connect(db_path) as connection:
            old_session = connection.execute(
                "SELECT f.feed_session_id FROM feed_session_items f "
                "JOIN feed_sessions s ON s.id=f.feed_session_id "
                "WHERE f.item_id=? AND s.profile='noah' AND s.shelf_id!='again'",
                (item["id"],),
            ).fetchone()[0]
        asyncio.run(db.kids_watch_event_record(
            video_id=item["video_id"], event="completed", profile="noah",
            position_seconds=None, session_id="", startup_ms=None,
            correlation_id="daily-owner-completion",
        ))
        stale = asyncio.run(db.kids_daily_library_get_or_create(
            day=datetime.now(timezone.utc).date().isoformat(), profile="noah",
            shelf_limit=72, proposed_item_ids=before_ids,
        ))
        assert all(item["id"] not in ids for shelf, ids in stale.items() if shelf != "again")
        after = _asset_item_ids(db_path, client.get(
            "/v1/kids/shelves", params={"profile": "noah"},
        ).json())
        assert after["again"] == [item["id"]]
        assert all(item["id"] not in ids for shelf, ids in after.items() if shelf != "again")
        old_page = client.get("/v1/kids/feed", params={
            "profile": "noah", "cursor": _encode_kids_cursor(old_session, 0, "noah"),
        })
        assert old_page.status_code == 200
        assert item["id"] not in _item_ids_for_assets(
            db_path, [entry["id"] for entry in old_page.json()["items"]],
        )
        felix = _asset_item_ids(db_path, client.get(
            "/v1/kids/shelves", params={"profile": "felix"},
        ).json())
        assert "again" not in felix
        assert item["id"] in {i for ids in felix.values() for i in ids}


def test_shelves_fill_initially_empty_daily_slots_without_reordering(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    db = asyncio.run(_seed_shelf_catalog(db_path, []))
    module = _load_app(tmp_path, monkeypatch)
    _mock_upstream(monkeypatch, module, [])

    with TestClient(module.app) as client:
        empty = client.get("/v1/kids/shelves", params={"profile": "noah"})
        assert empty.status_code == 200
        assert _asset_item_ids(db_path, empty.json())["learning-nl"] == []

        first_items = [
            asyncio.run(
                _ready_item(
                    db,
                    index,
                    language="nl",
                    content_kind="learning",
                    profile_slugs=["noah"],
                )
            )[1]
            for index in range(1, 4)
        ]
        filled = client.get("/v1/kids/shelves", params={"profile": "noah"})
        filled_ids = _asset_item_ids(db_path, filled.json())
        assert {
            item_id for shelf_ids in filled_ids.values() for item_id in shelf_ids
        } == {item["id"] for item in first_items}
        day = datetime.now(timezone.utc).date().isoformat()
        occupied = [row for row in _daily_rows(db_path, day, "noah") if row[2] is not None]

        asyncio.run(
            _ready_item(
                db,
                4,
                language="nl",
                content_kind="learning",
                profile_slugs=["noah"],
            )
        )
        extended = client.get("/v1/kids/shelves", params={"profile": "noah"})
        extended_ids = _asset_item_ids(db_path, extended.json())
        for shelf, item_ids in filled_ids.items():
            assert set(item_ids).issubset(extended_ids[shelf])
        assert set(occupied).issubset(_daily_rows(db_path, day, "noah"))
        assert sum(map(len, extended_ids.values())) == sum(
            map(len, filled_ids.values())
        ) + 1


def test_shelves_keep_materialized_gaps_after_revoke_and_block(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    specs = [
        (index, "nl", "learning", ["noah"])
        for index in range(1, 17)
    ]
    db = asyncio.run(_seed_shelf_catalog(db_path, specs))
    module = _load_app(tmp_path, monkeypatch)
    _mock_upstream(monkeypatch, module, [])

    with TestClient(module.app) as client:
        first = client.get("/v1/kids/shelves", params={"profile": "noah"}).json()
        first_ids = _asset_item_ids(db_path, first)["learning-nl"]
        assert len(first_ids) == 8
        revoked_id, blocked_id = first_ids[:2]
        learning = next(
            shelf for shelf in first["shelves"] if shelf["id"] == "learning-nl"
        )
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
            json={"asset_id": learning["items"][0]["id"]},
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
        current_ids = _asset_item_ids(db_path, current.json())["learning-nl"]
        assert current_ids == [
            item_id for item_id in first_ids if item_id not in {revoked_id, blocked_id}
        ]
        assert new_item["id"] in {
            item_id
            for shelf_ids in _asset_item_ids(db_path, current.json()).values()
            for item_id in shelf_ids
        }
        rows = _daily_rows(
            db_path,
            datetime.now(timezone.utc).date().isoformat(),
            "noah",
        )
        assert [
            row[2] for row in rows if row[0] == "learning-nl"
        ][:8] == first_ids
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(
                "SELECT state FROM relay_leases WHERE id=?",
                (lease_id,),
            ).fetchone()[0] == "revoked"
        assert client.post(
            "/v1/kids/playback-sessions",
            json={"asset_id": learning["items"][0]["id"]},
        ).status_code in {403, 409}
