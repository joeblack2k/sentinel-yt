from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.db import Database
from scripts.migrate_kids_to_sentinel import merge_kids_database


def _source_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE catalog_meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
        CREATE TABLE catalog_sources(
            id INTEGER PRIMARY KEY, kind TEXT, reference TEXT, title TEXT,
            safety_verdict TEXT, safety_reason TEXT, safety_checked_at TEXT,
            safety_policy_version TEXT, safety_evidence_json TEXT,
            safety_sample_count INTEGER, state TEXT, actor TEXT, changed_at TEXT,
            reason TEXT, revision INTEGER, correlation_id TEXT, language TEXT,
            content_kind TEXT
        );
        CREATE TABLE catalog_items(
            id INTEGER PRIMARY KEY, video_id TEXT, title TEXT, source_id INTEGER,
            channel_id TEXT, channel_title TEXT, thumbnail_url TEXT,
            duration_seconds INTEGER, visual_category TEXT, state TEXT, actor TEXT,
            changed_at TEXT, reason TEXT, revision INTEGER, correlation_id TEXT
        );
        CREATE TABLE catalog_transitions(
            id INTEGER PRIMARY KEY, entity_type TEXT, entity_id INTEGER,
            from_state TEXT, to_state TEXT, actor TEXT, changed_at TEXT,
            reason TEXT, revision INTEGER, correlation_id TEXT
        );
        CREATE TABLE kids_audit_events(
            id INTEGER PRIMARY KEY, event TEXT, entity_type TEXT, entity_id INTEGER,
            actor TEXT, reason TEXT, revision INTEGER, correlation_id TEXT, created_at TEXT
        );
        CREATE TABLE kids_watch_events(
            id INTEGER PRIMARY KEY, video_id TEXT, event TEXT, profile TEXT,
            position_seconds REAL, session_id TEXT, startup_ms INTEGER,
            correlation_id TEXT, created_at TEXT
        );
        CREATE TABLE kids_resolve_backlog(
            item_id INTEGER PRIMARY KEY, video_id TEXT, status TEXT,
            candidate_json TEXT, quality_height INTEGER, codec TEXT,
            resolved_at TEXT, expires_at TEXT, attempt_count INTEGER,
            next_attempt_at TEXT, last_error_code TEXT, updated_at TEXT
        );
        CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE feed_sessions(id TEXT PRIMARY KEY, profile TEXT, catalog_revision INTEGER,
            policy_version TEXT, created_at TEXT, expires_at TEXT);
        CREATE TABLE feed_session_items(feed_session_id TEXT, ordinal INTEGER, item_id INTEGER,
            asset_id TEXT);
        CREATE TABLE relay_leases(id TEXT PRIMARY KEY, item_id INTEGER, feed_session_id TEXT,
            state TEXT, candidate_json TEXT, quality_height INTEGER, revoked_reason TEXT,
            created_at TEXT, expires_at TEXT, heartbeat_at TEXT);
        """
    )
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    candidate = {
        "kind": "adaptive_mpv",
        "media_url": "https://video.example/1080",
        "audio_url": "https://audio.example/1080",
        "quality_height": 1080,
        "codec": "avc1",
        "video_headers": {},
        "audio_headers": {},
    }
    connection.execute("INSERT INTO catalog_meta VALUES('revision', 10)")
    connection.executemany(
        "INSERT INTO catalog_sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "channel", "UC-safe", "Safe", "SAFE", "", "now", "v1", "[]", 4,
             "approved", "donor", "now", "safe", 10, "source-safe", "en", "learning"),
            (2, "channel", "UC-blocked", "Blocked", "SAFE", "", "now", "v1", "[]", 4,
             "approved", "donor", "now", "safe", 10, "source-blocked", "en", "entertainment"),
        ],
    )
    connection.executemany(
        "INSERT INTO catalog_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "video-safe", "Safe video", 1, "UC-safe", "Safe", "https://i.ytimg.com/safe.jpg",
             300, "animals", "approved", "donor", "now", "safe", 10, "item-safe"),
            (2, "video-4k", "4K video", 1, "UC-safe", "Safe", "https://i.ytimg.com/4k.jpg",
             300, "science", "approved", "donor", "now", "safe", 10, "item-4k"),
            (3, "video-blocked", "Blocked video", 2, "UC-blocked", "Blocked", "https://i.ytimg.com/blocked.jpg",
             300, "general", "approved", "donor", "now", "safe", 10, "item-blocked"),
        ],
    )
    connection.executemany(
        "INSERT INTO kids_resolve_backlog VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "video-safe", "ready", json.dumps(candidate), 1080, "avc1", "now", future, 1, None, "", "now"),
            (2, "video-4k", "ready", json.dumps({**candidate, "quality_height": 2160}), 2160, "av1", "now", future, 1, None, "", "now"),
            (3, "video-blocked", "blocked", "", None, "", None, None, 0, None, "policy", "now"),
        ],
    )
    connection.execute(
        "INSERT INTO kids_watch_events VALUES(1,'video-safe','completed','noah',300,'old-session',800,'watch-1','now')"
    )
    connection.execute(
        "INSERT INTO feed_sessions VALUES('old-feed','noah',10,'v1','now',?)", (future,)
    )
    connection.commit()
    connection.close()


def _target_database(path):
    asyncio.run(Database(str(path)).init())


def test_merge_is_idempotent_preserves_target_decisions_and_excludes_ephemeral_state(tmp_path):
    source = tmp_path / "donor.db"
    target = tmp_path / "sentinel.db"
    _source_database(source)
    _target_database(target)

    with sqlite3.connect(target) as connection:
        connection.execute(
            """
            INSERT INTO catalog_sources(
                kind,reference,title,language,safety_verdict,state,actor,changed_at,
                reason,revision,correlation_id
            ) VALUES('channel','UC-blocked','Parent decision','en','UNSAFE','blocked',
                     'parent','now','explicit block',10,'parent-block')
            """
        )

    first = merge_kids_database(source, target)
    second = merge_kids_database(source, target)

    assert first == second
    assert first["row_counts"]["sources_inserted"] == 1
    assert first["row_counts"]["items_inserted"] == 3
    assert first["row_counts"]["backlog_ready_imported"] == 1
    assert first["excluded_ephemeral_rows"]["feed_sessions"] == 1

    with sqlite3.connect(target) as connection:
        connection.row_factory = sqlite3.Row
        blocked = connection.execute(
            "SELECT state,safety_verdict,title,content_kind FROM catalog_sources WHERE reference='UC-blocked'"
        ).fetchone()
        assert tuple(blocked) == ("blocked", "UNSAFE", "Parent decision", "entertainment")
        assert connection.execute(
            "SELECT content_kind FROM catalog_sources WHERE reference='UC-safe'"
        ).fetchone()[0] == "learning"
        profile = connection.execute(
            "SELECT value FROM settings WHERE key='kids_profile_max_age'"
        ).fetchone()
        if profile is not None:
            assert profile[0] == "6"
        assert connection.execute("SELECT COUNT(*) FROM feed_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM relay_leases").fetchone()[0] == 0
        rows = connection.execute(
            """
            SELECT b.status,b.candidate_json,b.quality_height
            FROM kids_resolve_backlog b JOIN catalog_items i ON i.id=b.item_id
            WHERE i.video_id IN ('video-safe','video-4k')
            ORDER BY i.video_id
            """
        ).fetchall()
        assert rows[0]["status"] == "pending"
        assert rows[0]["quality_height"] is None
        assert rows[1]["status"] == "ready"
        assert rows[1]["quality_height"] == 1080
        assert connection.execute("SELECT COUNT(*) FROM kids_watch_events").fetchone()[0] == 1
