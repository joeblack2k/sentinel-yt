import asyncio
import importlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.services.blocklists import BlocklistService
from app.services.judge import JudgeService
from app.services.kids_ingest import HOME_SOURCE_REFERENCE, ingest_once


CHANNEL_ID = f"UC{'a' * 22}"


def signed_url(kind: str, expires_at: datetime) -> str:
    return (
        f"https://rr1---sn.example.googlevideo.com/videoplayback/{kind}"
        f"?expire={int(expires_at.timestamp())}&sig=signature"
    )


def ready_candidate(quality_height: int = 1080) -> tuple[dict, str, str]:
    resolved_at = datetime.now(timezone.utc)
    expires_at = resolved_at + timedelta(hours=1)
    candidate = {
        "media_url": signed_url("video", expires_at),
        "audio_url": signed_url("audio", expires_at),
        "quality_height": quality_height,
        "codec": "avc1.640028",
        "video_headers": {},
        "audio_headers": {},
    }
    return candidate, resolved_at.isoformat(), expires_at.isoformat()


async def approved_source_and_item(
    db: Database,
    *,
    video_id: str,
    title: str,
    channel_title: str = "Netflix Jr",
) -> tuple[dict, dict]:
    source = await db.catalog_create(
        "source",
        {
            "kind": "channel",
            "reference": CHANNEL_ID,
            "title": channel_title,
            "correlation_id": f"source-{video_id}",
        },
    )
    await db.catalog_source_safety_update(
        source["id"],
        verdict="SAFE",
        language="nl",
        age_suitability={"2": "SUITABLE", "6": "SUITABLE"},
        reason="test",
        actor="guardian",
        correlation_id=f"safety-{video_id}",
        policy_version="sampled-channel-v1",
    )
    source = await db.catalog_transition(
        "source",
        source["id"],
        {
            "state": "approved",
            "actor": "parent",
            "reason": "approved",
            "correlation_id": f"approve-source-{video_id}",
        },
    )
    item = await db.catalog_create(
        "item",
        {
            "video_id": video_id,
            "title": title,
            "source_id": source["id"],
            "channel_id": CHANNEL_ID,
            "channel_title": channel_title,
            "correlation_id": f"item-{video_id}",
        },
    )
    item = await db.catalog_transition(
        "item",
        item["id"],
        {
            "state": "approved",
            "actor": "parent",
            "reason": "approved",
            "correlation_id": f"approve-item-{video_id}",
        },
    )
    return source, item


async def persist_ready(db: Database, item_id: int, quality_height: int = 1080) -> None:
    candidate, resolved_at, expires_at = ready_candidate(quality_height)
    await db.kids_resolve_success(
        item_id=item_id,
        candidate=candidate,
        quality_height=quality_height,
        codec="avc1.640028",
        resolved_at=resolved_at,
        expires_at=expires_at,
    )


@pytest.mark.asyncio
async def test_reconcile_blocks_existing_item_idempotently_without_touching_source(tmp_path):
    db_path = tmp_path / "sentinel.db"
    settings = Settings(db_path=str(db_path), data_dir=str(tmp_path / "data"))
    db = Database(str(db_path))
    await db.init()
    source, item = await approved_source_and_item(
        db,
        video_id="deny0000001",
        title="Blocked test item",
    )
    await persist_ready(db, item["id"])
    blocklists = BlocklistService(settings)
    await blocklists.save_local_content("video:deny0000001 | configured block\n")
    await blocklists.reload(db)
    judge = JudgeService(db, blocklists=blocklists)

    assert await judge.reconcile_catalog_policy() == 1
    assert await judge.reconcile_catalog_policy() == 0

    stored_item = await db.catalog_item_by_video("deny0000001")
    stored_source = await db.catalog_get("source", source["id"])
    assert stored_item["state"] == "blocked"
    assert stored_source["state"] == "approved"
    assert await db.kids_playback_authorization(
        "deny0000001",
        minimum_remaining_seconds=300,
    ) is None
    resolve_row = next(
        row for row in await db.kids_resolve_recent_rows() if row["item_id"] == item["id"]
    )
    assert resolve_row["status"] == "blocked"
    assert resolve_row["quality_height"] is None

    with sqlite3.connect(db.db_path) as connection:
        transition = connection.execute(
            """
            SELECT from_state,to_state,actor,reason,correlation_id
            FROM catalog_transitions
            WHERE entity_type='item' AND entity_id=?
            ORDER BY id DESC
            """,
            (item["id"],),
        ).fetchone()
    assert transition == (
        "approved",
        "blocked",
        "kids-guardian-blocklist",
        "Blocked by file blocklist (video)",
        "kids-blocklist-item-deny0000001",
    )

    await blocklists.save_local_content("# block removed by parent\n")
    await blocklists.reload(db)
    assert await judge.reconcile_catalog_policy() == 0
    assert (await db.catalog_item_by_video("deny0000001"))["state"] == "approved"
    restored_row = next(
        row for row in await db.kids_resolve_recent_rows() if row["item_id"] == item["id"]
    )
    assert restored_row["status"] == "pending"


@pytest.mark.asyncio
async def test_reconcile_catalog_policy_snapshots_rules_and_flags(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    settings = Settings(db_path=str(db_path), data_dir=str(tmp_path / "data"))
    db = Database(str(db_path))
    await db.init()
    _, item = await approved_source_and_item(
        db,
        video_id="bulkdeny00001",
        title="Bulk blocked item",
    )
    await db.add_rule(
        "blacklist",
        "video",
        "bulkdeny00001",
        label="bulk test",
        source_list="manual",
    )
    blocklists = BlocklistService(settings)
    await blocklists.reload(db)
    judge = JudgeService(
        db,
        blocklists=blocklists,
    )

    async def unexpected_rule_lookup(*args, **kwargs):
        raise AssertionError("bulk reconcile must not query rules per catalog row")

    original_get_setting = db.get_setting
    policy_reads = 0

    async def counted_get_setting(key):
        nonlocal policy_reads
        if key == "policy_flags_json":
            policy_reads += 1
        return await original_get_setting(key)

    monkeypatch.setattr(db, "find_rule_match", unexpected_rule_lookup)
    monkeypatch.setattr(db, "get_setting", counted_get_setting)

    assert await judge.reconcile_catalog_policy() == 1
    assert policy_reads == 1
    assert (await db.catalog_item_by_video("bulkdeny00001"))["state"] == "blocked"


@pytest.mark.asyncio
async def test_channel_entry_from_blocklist_blocks_source_and_catalog_item(tmp_path):
    db_path = tmp_path / "sentinel.db"
    settings = Settings(db_path=str(db_path), data_dir=str(tmp_path / "data"))
    db = Database(str(db_path))
    await db.init()
    source, item = await approved_source_and_item(
        db,
        video_id="deny0000005",
        title="Channel blocked item",
    )
    blocklists = BlocklistService(settings)
    await blocklists.save_local_content(f"channel:{CHANNEL_ID} | configured channel block\n")
    await blocklists.reload(db)
    judge = JudgeService(db, blocklists=blocklists)

    assert await judge.reconcile_catalog_policy() == 2
    assert (await db.catalog_get("source", source["id"]))["state"] == "blocked"
    assert (await db.catalog_item_by_video("deny0000005"))["state"] == "blocked"
    assert await db.catalog_items_list() == []


@dataclass
class BlocklistedBrowser:
    async def cards_for_source(self, kind: str, reference: str) -> list[dict]:
        card = {
            "href": "/watch?v=deny0000002",
            "title": "Blocked test item",
            "label": "Blocked test item by Netflix Jr 10 views",
            "channel_title": "Netflix Jr",
            "duration": "5:00",
            "thumbnail_url": "https://i.ytimg.com/vi/deny0000002/hqdefault.jpg",
            "channel_id": CHANNEL_ID,
        }
        if reference in {HOME_SOURCE_REFERENCE, CHANNEL_ID}:
            return [card]
        return []


class UnexpectedClassifier:
    async def classify(self, _metadata):
        raise AssertionError("deterministic blocklist must run before item approval")


@pytest.mark.asyncio
async def test_ingest_honors_local_blocklist_before_channel_inheritance(tmp_path):
    db_path = tmp_path / "sentinel.db"
    settings = Settings(db_path=str(db_path), data_dir=str(tmp_path / "data"))
    db = Database(str(db_path))
    await db.init()
    source, existing = await approved_source_and_item(
        db,
        video_id="safe0000001",
        title="Existing safe item",
    )
    await db.catalog_transition(
        "item",
        existing["id"],
        {
            "state": "revoked",
            "actor": "test",
            "reason": "fixture",
            "correlation_id": "revoke-fixture",
        },
    )

    blocklists = BlocklistService(settings)
    await blocklists.save_local_content("video:deny0000002 | configured block\n")
    await blocklists.reload(db)
    judge = JudgeService(db, blocklists=blocklists)
    report = await ingest_once(
        db,
        BlocklistedBrowser(),
        UnexpectedClassifier(),
        channel_policy_version="sampled-channel-v1",
        judge=judge,
    )

    item = await db.catalog_item_by_video("deny0000002")
    assert report.approved == 0
    assert report.blocked == 1
    assert item["state"] == "blocked"
    assert item["reason"] == "Blocked by file blocklist (video)"
    assert (await db.catalog_get("source", source["id"]))["state"] == "approved"
    assert await db.catalog_items_list() == []


@pytest.mark.asyncio
async def test_feed_and_playback_hide_360_until_a_720_candidate_is_ready(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    _source, item = await approved_source_and_item(
        db,
        video_id="quality0001",
        title="Calm educational episode",
    )
    await persist_ready(db, item["id"], quality_height=360)

    assert await db.kids_eligible_feed_list(300) == []
    assert await db.kids_playback_authorization(
        "quality0001",
        minimum_remaining_seconds=300,
    ) is None

    await db.kids_resolve_sync_backlog(minimum_quality_height=720)
    row = (await db.kids_resolve_recent_rows())[0]
    assert row["status"] == "pending"
    assert row["quality_height"] is None

    await db.kids_resolve_claim_due(limit=1, refresh_margin_seconds=300)
    await persist_ready(db, item["id"], quality_height=720)
    assert [entry["video_id"] for entry in await db.kids_eligible_feed_list(300)] == ["quality0001"]
    assert await db.kids_playback_authorization(
        "quality0001",
        minimum_remaining_seconds=300,
    )


def test_feed_reconciles_live_blocklist_rows_without_ingest(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    monkeypatch.setenv("SENTINEL_DB_PATH", str(db_path))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    module = importlib.reload(importlib.import_module("app.main"))

    db = Database(str(db_path))
    asyncio.run(db.init())
    source, item = asyncio.run(
        approved_source_and_item(
            db,
            video_id="deny0000003",
            title="Blocked legacy approval",
        )
    )
    asyncio.run(persist_ready(db, item["id"]))

    async def available() -> bool:
        return True

    async def disabled() -> bool:
        return False

    with TestClient(module.app) as client:
        runtime = client.app.state.runtime
        asyncio.run(
            runtime.blocklists.save_local_content(
                "video:deny0000003 | configured block\n"
            )
        )
        asyncio.run(runtime.blocklists.reload(runtime.db))
        monkeypatch.setattr(runtime, "monitoring_enabled_now", available)
        monkeypatch.setattr(runtime.db, "kids_kill_switch_enabled", disabled)
        response = client.get("/api/kids/catalog/items")

    assert response.status_code == 200
    assert response.json() == {"state": "ready", "items": []}
    assert asyncio.run(db.catalog_item_by_video("deny0000003"))["state"] == "blocked"
    assert asyncio.run(db.catalog_get("source", source["id"]))["state"] == "approved"


def test_catalog_approval_rejects_item_blocked_by_live_blocklist(tmp_path, monkeypatch):
    db_path = tmp_path / "sentinel.db"
    monkeypatch.setenv("SENTINEL_DB_PATH", str(db_path))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    module = importlib.reload(importlib.import_module("app.main"))

    db = Database(str(db_path))
    asyncio.run(db.init())
    source, item = asyncio.run(
        approved_source_and_item(
            db,
            video_id="deny0000004",
            title="Blocked candidate",
        )
    )
    asyncio.run(
        db.catalog_transition(
            "item",
            item["id"],
            {
                "state": "candidate",
                "actor": "test",
                "reason": "reset to candidate",
                "correlation_id": "reset-candidate",
            },
        )
    )

    payload = {
        "state": "approved",
        "actor": "parent",
        "reason": "approve",
        "correlation_id": "approve-blocked",
    }
    with TestClient(module.app) as client:
        runtime = client.app.state.runtime
        asyncio.run(
            runtime.blocklists.save_local_content(
                "video:deny0000004 | configured block\n"
            )
        )
        asyncio.run(runtime.blocklists.reload(runtime.db))
        response = client.patch(
            f"/api/kids/catalog/items/{item['id']}/state",
            json=payload,
        )

    assert response.status_code == 409
    assert asyncio.run(db.catalog_item_by_video("deny0000004"))["state"] == "candidate"
