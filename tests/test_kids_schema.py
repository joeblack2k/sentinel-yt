import json
import sqlite3

import pytest

from app.db import Database


async def _approved_source_and_item(db: Database, suffix: str = "schema") -> tuple[dict, dict]:
    channel_id = f"schema-channel-{suffix}"
    source = await db.catalog_create(
        "source",
        {
            "kind": "channel",
            "reference": channel_id,
            "title": "Schema channel",
            "language": "nl",
            "correlation_id": f"source-{suffix}",
        },
    )
    source = await db.catalog_source_safety_update(
        source["id"],
        verdict="SAFE",
        language="nl",
        reason="schema setup",
        actor="test",
        correlation_id=f"safety-{suffix}",
    )
    source = await db.catalog_transition(
        "source",
        source["id"],
        {
            "state": "approved",
            "actor": "test",
            "reason": "schema setup",
            "correlation_id": f"approve-source-{suffix}",
        },
    )
    item = await db.catalog_create(
        "item",
        {
            "video_id": f"schema-video-{suffix}",
            "title": "Schema video",
            "source_id": source["id"],
            "channel_id": channel_id,
            "channel_title": "Schema channel",
            "correlation_id": f"item-{suffix}",
        },
    )
    item = await db.catalog_transition(
        "item",
        item["id"],
        {
            "state": "approved",
            "actor": "test",
            "reason": "schema setup",
            "correlation_id": f"approve-item-{suffix}",
        },
    )
    return source, item


def _insert_feed_item_and_lease(
    db_path: str,
    *,
    item_id: int,
    quality_height: int,
    lease_id: str,
    feed_session_id: str,
    candidate_quality_height: int | None = None,
) -> None:
    candidate_quality_height = candidate_quality_height or quality_height
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO feed_sessions(
                id, profile, catalog_revision, policy_version, created_at, expires_at
            ) VALUES (?, 'noah', 1, 'test', 'now', 'later')
            """,
            (feed_session_id,),
        )
        connection.execute(
            """
            INSERT INTO feed_session_items(feed_session_id, ordinal, item_id, asset_id)
            VALUES (?, 0, ?, ?)
            """,
            (feed_session_id, item_id, f"asset-{lease_id}"),
        )
        connection.execute(
            """
            INSERT INTO relay_leases(
                id, item_id, feed_session_id, state, candidate_json, quality_height,
                created_at, expires_at, heartbeat_at
            ) VALUES (?, ?, ?, 'active', ?, ?, 'now', 'later', 'now')
            """,
            (
                lease_id,
                item_id,
                feed_session_id,
                json.dumps({"quality_height": candidate_quality_height}),
                quality_height,
            ),
        )
        connection.commit()


def _lease_state(db_path: str, lease_id: str) -> tuple[str, str]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT state, revoked_reason FROM relay_leases WHERE id=?",
            (lease_id,),
        ).fetchone()


@pytest.mark.asyncio
async def test_kids_schema_init_is_idempotent_with_language_and_defaults(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))

    await db.init()
    assert await db.kids_kill_switch_enabled() is True

    with sqlite3.connect(db.db_path) as connection:
        source_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(catalog_sources)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "language" in source_columns
    assert {"feed_sessions", "feed_session_items", "relay_leases"} <= tables

    await db.init()
    assert await db.kids_kill_switch_enabled() is True


@pytest.mark.asyncio
async def test_kids_schema_preserves_existing_values_and_rejects_invalid_relay_quality(
    tmp_path,
):
    db_path = tmp_path / "sentinel.db"
    db = Database(str(db_path))
    await db.init()
    await db.set_setting("kids_kill_switch", "false")
    await db.init()
    assert await db.kids_kill_switch_enabled() is False

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO feed_sessions(
                id, catalog_revision, policy_version, created_at, expires_at
            ) VALUES ('feed-1', 1, 'test', 'now', 'later')
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO relay_leases(
                    id, item_id, feed_session_id, candidate_json, quality_height,
                    created_at, expires_at, heartbeat_at
                ) VALUES ('lease-360', 1, 'feed-1', '{"quality_height":360}', 360,
                          'now', 'later', 'now')
                """
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO relay_leases(
                    id, item_id, feed_session_id, candidate_json, quality_height,
                    created_at, expires_at, heartbeat_at
                ) VALUES ('lease-2160', 1, 'feed-1', '{"quality_height":2160}', 2160,
                          'now', 'later', 'now')
                """
            )


@pytest.mark.asyncio
async def test_kids_schema_accepts_720p_and_1080p_and_rejects_mismatch(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    _source, item = await _approved_source_and_item(db, "quality")

    _insert_feed_item_and_lease(
        db.db_path,
        item_id=item["id"],
        quality_height=720,
        lease_id="lease-720",
        feed_session_id="feed-720",
    )
    _insert_feed_item_and_lease(
        db.db_path,
        item_id=item["id"],
        quality_height=1080,
        lease_id="lease-1080",
        feed_session_id="feed-1080",
    )

    with pytest.raises(sqlite3.IntegrityError):
        _insert_feed_item_and_lease(
            db.db_path,
            item_id=item["id"],
            quality_height=720,
            candidate_quality_height=1080,
            lease_id="lease-mismatch",
            feed_session_id="feed-mismatch",
        )
    with pytest.raises(sqlite3.IntegrityError):
        _insert_feed_item_and_lease(
            db.db_path,
            item_id=item["id"],
            quality_height=2160,
            lease_id="lease-2160",
            feed_session_id="feed-2160",
        )

    with sqlite3.connect(db.db_path) as connection:
        rows = connection.execute(
            "SELECT quality_height FROM relay_leases ORDER BY quality_height"
        ).fetchall()
    assert rows == [(720,), (1080,)]


@pytest.mark.asyncio
async def test_source_language_is_validated_and_revoke_revokes_active_leases(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    source, item = await _approved_source_and_item(db, "revoke-item")

    with pytest.raises(ValueError, match="invalid source language"):
        await db.catalog_source_safety_update(
            source["id"],
            verdict="SAFE",
            language="fr",
            reason="invalid test language",
            actor="test",
            correlation_id="invalid-language",
        )

    _insert_feed_item_and_lease(
        db.db_path,
        item_id=item["id"],
        quality_height=1080,
        lease_id="lease-item-revoke",
        feed_session_id="feed-item-revoke",
    )
    await db.catalog_transition(
        "item",
        item["id"],
        {
            "state": "revoked",
            "actor": "parent",
            "reason": "item revoked",
            "correlation_id": "item-revoked",
        },
    )
    assert _lease_state(db.db_path, "lease-item-revoke") == (
        "revoked",
        "item revoked",
    )

    source, item = await _approved_source_and_item(db, "revoke-source")
    _insert_feed_item_and_lease(
        db.db_path,
        item_id=item["id"],
        quality_height=720,
        lease_id="lease-source-revoke",
        feed_session_id="feed-source-revoke",
    )
    await db.catalog_transition(
        "source",
        source["id"],
        {
            "state": "revoked",
            "actor": "parent",
            "reason": "source revoked",
            "correlation_id": "source-revoked",
        },
    )
    assert _lease_state(db.db_path, "lease-source-revoke") == (
        "revoked",
        "source revoked",
    )
