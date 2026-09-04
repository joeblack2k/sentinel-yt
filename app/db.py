from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from .config import DEFAULT_POLICY_FLAGS, get_host_timezone_name
from .services.kids_catalog import KIDS_HOME_SOURCE_REFERENCE
from .services.kids_database import KidsDatabaseMixin
from .services.time_utils import utc_now_iso


class Database(KidsDatabaseMixin):
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    start TEXT NOT NULL,
                    end TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'blocklist',
                    created_at TEXT,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_type TEXT,
                    scope TEXT,
                    value TEXT,
                    label TEXT DEFAULT '',
                    url TEXT DEFAULT '',
                    source_list TEXT DEFAULT 'manual',
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS catalog_meta (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('channel', 'playlist')),
                    reference TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    avatar_url TEXT NOT NULL DEFAULT '',
                    poster_item_id INTEGER,
                    language TEXT NOT NULL DEFAULT 'unknown'
                        CHECK(language IN ('nl', 'en', 'mixed', 'unknown')),
                    content_kind TEXT NOT NULL DEFAULT 'unknown'
                        CHECK(content_kind IN ('learning', 'entertainment', 'mixed', 'unknown')),
                    safety_verdict TEXT NOT NULL DEFAULT 'UNCERTAIN',
                    safety_reason TEXT NOT NULL DEFAULT '',
                    safety_checked_at TEXT,
                    safety_policy_version TEXT NOT NULL DEFAULT '',
                    safety_evidence_json TEXT NOT NULL DEFAULT '[]',
                    safety_sample_count INTEGER NOT NULL DEFAULT 0,
                    age_suitability_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL DEFAULT 'candidate'
                        CHECK(state IN ('candidate', 'approved', 'blocked', 'revoked', 'unknown')),
                    actor TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    correlation_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kids_profiles (
                    slug TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    age_years INTEGER NOT NULL CHECK(age_years BETWEEN 0 AND 18),
                    avatar_key TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_source_profiles (
                    source_id INTEGER NOT NULL REFERENCES catalog_sources(id) ON DELETE CASCADE,
                    profile_slug TEXT NOT NULL REFERENCES kids_profiles(slug) ON DELETE CASCADE,
                    actor TEXT NOT NULL DEFAULT 'system',
                    reason TEXT NOT NULL DEFAULT '',
                    assigned_at TEXT NOT NULL,
                    PRIMARY KEY(source_id, profile_slug)
                );
                CREATE INDEX IF NOT EXISTS idx_catalog_source_profiles_profile
                    ON catalog_source_profiles(profile_slug, source_id);
                CREATE TABLE IF NOT EXISTS catalog_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    source_id INTEGER REFERENCES catalog_sources(id),
                    channel_id TEXT NOT NULL DEFAULT '',
                    channel_title TEXT NOT NULL DEFAULT '',
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    visual_category TEXT NOT NULL DEFAULT 'general',
                    state TEXT NOT NULL DEFAULT 'candidate'
                        CHECK(state IN ('candidate', 'approved', 'blocked', 'revoked', 'unknown')),
                    actor TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    correlation_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    correlation_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kids_audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    entity_type TEXT NOT NULL DEFAULT '',
                    entity_id INTEGER,
                    actor TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 0,
                    correlation_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kids_audit_created ON kids_audit_events(id DESC);
                CREATE TABLE IF NOT EXISTS kids_watch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    event TEXT NOT NULL CHECK(event IN ('selected', 'started', 'completed', 'stopped')),
                    profile TEXT NOT NULL DEFAULT 'noah',
                    position_seconds REAL,
                    session_id TEXT NOT NULL DEFAULT '',
                    startup_ms INTEGER,
                    correlation_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kids_watch_created ON kids_watch_events(id DESC);
                CREATE INDEX IF NOT EXISTS idx_kids_watch_video ON kids_watch_events(video_id, id DESC);
                CREATE TABLE IF NOT EXISTS kids_resolve_backlog (
                    item_id INTEGER PRIMARY KEY REFERENCES catalog_items(id),
                    video_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK(status IN ('pending','running','ready','retry','blocked')),
                    candidate_json TEXT NOT NULL DEFAULT '',
                    quality_height INTEGER,
                    codec TEXT NOT NULL DEFAULT '',
                    resolved_at TEXT,
                    expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_kids_resolve_due ON kids_resolve_backlog(status, next_attempt_at);
                CREATE INDEX IF NOT EXISTS idx_kids_resolve_expiry ON kids_resolve_backlog(expires_at);

                CREATE TABLE IF NOT EXISTS feed_sessions (
                    id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL DEFAULT 'noah',
                    source_id INTEGER,
                    catalog_revision INTEGER NOT NULL CHECK(catalog_revision >= 0),
                    policy_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS kids_daily_library (
                    day TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    shelf TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                    item_id INTEGER REFERENCES catalog_items(id) ON DELETE SET NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(day, profile, shelf, ordinal)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS feed_session_items (
                    feed_session_id TEXT NOT NULL REFERENCES feed_sessions(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                    item_id INTEGER NOT NULL REFERENCES catalog_items(id) ON DELETE CASCADE,
                    asset_id TEXT NOT NULL,
                    PRIMARY KEY(feed_session_id, ordinal),
                    UNIQUE(feed_session_id, item_id),
                    UNIQUE(feed_session_id, asset_id)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS relay_leases (
                    id TEXT PRIMARY KEY,
                    item_id INTEGER NOT NULL REFERENCES catalog_items(id) ON DELETE CASCADE,
                    feed_session_id TEXT NOT NULL REFERENCES feed_sessions(id) ON DELETE CASCADE,
                    state TEXT NOT NULL DEFAULT 'active'
                        CHECK(state IN ('active', 'revoked', 'expired', 'closed')),
                    candidate_json TEXT NOT NULL CHECK(json_valid(candidate_json)),
                    quality_height INTEGER NOT NULL CHECK(quality_height BETWEEN 720 AND 1080),
                    revoked_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    CHECK(
                        CASE WHEN json_valid(candidate_json) THEN
                            COALESCE(
                                json_type(candidate_json, '$.quality_height') = 'integer'
                                AND json_extract(candidate_json, '$.quality_height')
                                    BETWEEN 720 AND 1080
                                AND json_extract(candidate_json, '$.quality_height') = quality_height,
                                0
                            )
                        ELSE 0 END
                    )
                ) WITHOUT ROWID;

                CREATE INDEX IF NOT EXISTS idx_feed_sessions_expires
                    ON feed_sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_feed_session_assets
                    ON feed_session_items(asset_id);
                CREATE INDEX IF NOT EXISTS idx_relay_leases_active
                    ON relay_leases(state, expires_at);
                CREATE INDEX IF NOT EXISTS idx_rules_scope_value ON rules(scope, value);
                CREATE INDEX IF NOT EXISTS idx_rules_type_scope ON rules(rule_type, scope);
                CREATE INDEX IF NOT EXISTS idx_schedules_enabled_id ON schedules(enabled, id);
                """
            )
            await db.commit()
        await self._migrate_schema()
        await self._ensure_kids_profile_defaults()

        await self._ensure_defaults()
        await self._ensure_default_schedule_entry()

    async def _migrate_schema(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("PRAGMA table_info(rules)")
            cols = {row[1] for row in await cur.fetchall()}
            if "label" not in cols:
                await db.execute("ALTER TABLE rules ADD COLUMN label TEXT DEFAULT ''")
            if "url" not in cols:
                await db.execute("ALTER TABLE rules ADD COLUMN url TEXT DEFAULT ''")
            if "source_list" not in cols:
                await db.execute("ALTER TABLE rules ADD COLUMN source_list TEXT DEFAULT 'manual'")

            cur = await db.execute("PRAGMA table_info(schedules)")
            sched_cols = {row[1] for row in await cur.fetchall()}
            if sched_cols:
                if "name" not in sched_cols:
                    await db.execute("ALTER TABLE schedules ADD COLUMN name TEXT NOT NULL DEFAULT ''")
                if "mode" not in sched_cols:
                    await db.execute("ALTER TABLE schedules ADD COLUMN mode TEXT NOT NULL DEFAULT 'blocklist'")
                if "updated_at" not in sched_cols:
                    await db.execute("ALTER TABLE schedules ADD COLUMN updated_at TEXT")
            cur = await db.execute("PRAGMA table_info(catalog_sources)")
            source_cols = {row[1] for row in await cur.fetchall()}
            if "language" not in source_cols:
                await db.execute(
                    """
                    ALTER TABLE catalog_sources
                    ADD COLUMN language TEXT NOT NULL DEFAULT 'unknown'
                        CHECK(language IN ('nl', 'en', 'mixed', 'unknown'))
                    """
                )
            if "content_kind" not in source_cols:
                await db.execute(
                    """
                    ALTER TABLE catalog_sources
                    ADD COLUMN content_kind TEXT NOT NULL DEFAULT 'unknown'
                        CHECK(content_kind IN ('learning', 'entertainment', 'mixed', 'unknown'))
                    """
                )
            if "avatar_url" not in source_cols:
                await db.execute(
                    "ALTER TABLE catalog_sources ADD COLUMN avatar_url TEXT NOT NULL DEFAULT ''"
                )
            if "poster_item_id" not in source_cols:
                await db.execute(
                    "ALTER TABLE catalog_sources ADD COLUMN poster_item_id INTEGER"
                )
            cur = await db.execute("PRAGMA table_info(catalog_items)")
            item_cols = {row[1] for row in await cur.fetchall()}
            if "thumbnail_url" not in item_cols:
                await db.execute("ALTER TABLE catalog_items ADD COLUMN thumbnail_url TEXT NOT NULL DEFAULT ''")
            if "duration_seconds" not in item_cols:
                await db.execute("ALTER TABLE catalog_items ADD COLUMN duration_seconds INTEGER NOT NULL DEFAULT 0")
            if "visual_category" not in item_cols:
                await db.execute("ALTER TABLE catalog_items ADD COLUMN visual_category TEXT NOT NULL DEFAULT 'general'")
            if "channel_id" not in item_cols:
                await db.execute("ALTER TABLE catalog_items ADD COLUMN channel_id TEXT NOT NULL DEFAULT ''")
            if "channel_title" not in item_cols:
                await db.execute("ALTER TABLE catalog_items ADD COLUMN channel_title TEXT NOT NULL DEFAULT ''")
            # Existing catalog rows predate source identity fields; backfill them
            # from the already-approved source instead of invalidating the catalog.
            await db.execute(
                """
                UPDATE catalog_items
                SET channel_id=CASE
                        WHEN trim(coalesce(channel_id,''))='' THEN coalesce(
                            (
                                SELECT CASE
                                    WHEN kind='channel' AND reference!=? THEN reference
                                    ELSE ''
                                END
                                FROM catalog_sources WHERE id=catalog_items.source_id
                            ),
                            ''
                        )
                        ELSE channel_id
                    END,
                    channel_title=CASE
                        WHEN trim(coalesce(channel_title,''))='' THEN coalesce(
                            (SELECT title FROM catalog_sources WHERE id=catalog_items.source_id),
                            ''
                        )
                        ELSE channel_title
                    END
                WHERE source_id IS NOT NULL
                  AND (
                      trim(coalesce(channel_id,''))=''
                      OR trim(coalesce(channel_title,''))=''
                  )
                """,
                (KIDS_HOME_SOURCE_REFERENCE,),
            )
            cur = await db.execute("PRAGMA table_info(kids_watch_events)")
            watch_cols = {row[1] for row in await cur.fetchall()}
            if "startup_ms" not in watch_cols:
                await db.execute("ALTER TABLE kids_watch_events ADD COLUMN startup_ms INTEGER")
            if "safety_verdict" not in source_cols:
                await db.execute(
                    "ALTER TABLE catalog_sources ADD COLUMN safety_verdict TEXT NOT NULL DEFAULT 'UNCERTAIN'"
                )
            if "safety_reason" not in source_cols:
                await db.execute(
                    "ALTER TABLE catalog_sources ADD COLUMN safety_reason TEXT NOT NULL DEFAULT ''"
                )
            if "safety_checked_at" not in source_cols:
                await db.execute("ALTER TABLE catalog_sources ADD COLUMN safety_checked_at TEXT")
            if "safety_policy_version" not in source_cols:
                await db.execute(
                    "ALTER TABLE catalog_sources ADD COLUMN safety_policy_version TEXT NOT NULL DEFAULT ''"
                )
            if "safety_evidence_json" not in source_cols:
                await db.execute(
                    "ALTER TABLE catalog_sources ADD COLUMN safety_evidence_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "safety_sample_count" not in source_cols:
                await db.execute(
                    "ALTER TABLE catalog_sources ADD COLUMN safety_sample_count INTEGER NOT NULL DEFAULT 0"
                )
            if "age_suitability_json" not in source_cols:
                await db.execute(
                    "ALTER TABLE catalog_sources ADD COLUMN age_suitability_json TEXT NOT NULL DEFAULT '{}'"
                )
            cur = await db.execute("PRAGMA table_info(feed_sessions)")
            feed_session_cols = {row[1] for row in await cur.fetchall()}
            if "source_id" not in feed_session_cols:
                await db.execute("ALTER TABLE feed_sessions ADD COLUMN source_id INTEGER")

            cur = await db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='kids_daily_library'"
            )
            daily_schema_row = await cur.fetchone()
            daily_schema = str(daily_schema_row[0] or "").lower() if daily_schema_row else ""
            if "shelf in ('learning', 'fun', 'again')" in daily_schema:
                # Daily shelves are derived and their old names no longer map cleanly.
                await db.execute(
                    "ALTER TABLE kids_daily_library RENAME TO kids_daily_library_legacy"
                )
                await db.execute(
                    """
                    CREATE TABLE kids_daily_library (
                        day TEXT NOT NULL,
                        profile TEXT NOT NULL,
                        shelf TEXT NOT NULL,
                        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                        item_id INTEGER REFERENCES catalog_items(id) ON DELETE SET NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(day, profile, shelf, ordinal)
                    ) WITHOUT ROWID
                    """
                )
                await db.execute("DROP TABLE kids_daily_library_legacy")

            await db.commit()

    async def _ensure_defaults(self) -> None:
        defaults = {
            "active": "true",
            "schedule_enabled": "true",
            "schedule_start": "07:00",
            "schedule_end": "19:00",
            "timezone": get_host_timezone_name(),
            "failure_webhook_url": "",
            "judge_ok": "true",
            "last_error": "",
            "policy_flags_json": json.dumps(DEFAULT_POLICY_FLAGS, separators=(",", ":")),
            "blocklist_source_urls": "",
            "schedule_mode": "blocklist",
            "kids_kill_switch": "true",
            "kids_resolver_last_success_at": "",
        }
        for key, value in defaults.items():
            existing = await self.get_setting(key)
            if key == "policy_flags_json" and existing is not None:
                try:
                    parsed = json.loads(existing)
                except (TypeError, json.JSONDecodeError):
                    parsed = None
                if not isinstance(parsed, dict) or not parsed:
                    existing = None
            if existing is None:
                await self.set_setting(key, value)

    async def _ensure_default_schedule_entry(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            count_row = await (await db.execute("SELECT COUNT(*) FROM schedules")).fetchone()
            count = int(count_row[0]) if count_row else 0
            if count > 0:
                return
        enabled = ((await self.get_setting("schedule_enabled")) or "true") == "true"
        start = (await self.get_setting("schedule_start")) or "07:00"
        end = (await self.get_setting("schedule_end")) or "19:00"
        timezone_name = (await self.get_setting("timezone")) or get_host_timezone_name()
        mode = (await self.get_setting("schedule_mode")) or "blocklist"
        await self.add_schedule(
            name="Default",
            enabled=enabled,
            start=start,
            end=end,
            timezone=timezone_name,
            mode=mode,
        )

    async def get_setting(self, key: str) -> Optional[str]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = await cur.fetchone()
        return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO settings(key, value)
                VALUES(?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            await db.commit()

    async def all_settings(self) -> dict[str, str]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT key, value FROM settings")
            rows = await cur.fetchall()
        return {k: v for k, v in rows}

    async def list_schedules(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT id, name, enabled, start, end, timezone, mode, created_at, updated_at
                FROM schedules
                ORDER BY id ASC
                """
            )
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "id": int(row[0]),
                    "name": row[1] or "",
                    "enabled": bool(row[2]),
                    "start": row[3],
                    "end": row[4],
                    "timezone": row[5],
                    "mode": row[6] or "blocklist",
                    "created_at": row[7] or "",
                    "updated_at": row[8] or "",
                }
            )
        return out

    async def add_schedule(
        self,
        *,
        name: str,
        enabled: bool,
        start: str,
        end: str,
        timezone: str,
        mode: str,
    ) -> int:
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                INSERT INTO schedules(name, enabled, start, end, timezone, mode, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (name.strip(), 1 if enabled else 0, start, end, timezone, mode, now, now),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def update_schedule(
        self,
        schedule_id: int,
        *,
        name: str,
        enabled: bool,
        start: str,
        end: str,
        timezone: str,
        mode: str,
    ) -> bool:
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                UPDATE schedules
                SET name = ?, enabled = ?, start = ?, end = ?, timezone = ?, mode = ?, updated_at = ?
                WHERE id = ?
                """,
                (name.strip(), 1 if enabled else 0, start, end, timezone, mode, now, schedule_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def delete_schedule(self, schedule_id: int) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            await db.commit()
            return cur.rowcount > 0

    async def add_rule(
        self,
        rule_type: str,
        scope: str,
        value: str,
        *,
        label: str = "",
        url: str = "",
        source_list: str = "manual",
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO rules(rule_type, scope, value, label, url, source_list, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (rule_type, scope, value, label, url, source_list, utc_now_iso()),
            )
            await db.commit()

    async def delete_rule(self, rule_id: int) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
            await db.commit()

    async def get_rule(self, rule_id: int) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT id, rule_type, scope, value, label, url, source_list, created_at FROM rules WHERE id = ?",
                (rule_id,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "rule_type": row[1],
            "scope": row[2],
            "value": row[3],
            "label": row[4] or "",
            "url": row[5] or "",
            "source_list": row[6] or "manual",
            "created_at": row[7],
        }

    async def list_rules(
        self,
        *,
        limit: int | None = 200,
        rule_type: str | None = None,
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            query = (
                "SELECT id, rule_type, scope, value, label, url, source_list, created_at "
                "FROM rules"
            )
            args: tuple[Any, ...] = ()
            if rule_type in {"whitelist", "blacklist"}:
                query += " WHERE rule_type = ?"
                args = (rule_type,)
            query += " ORDER BY id DESC"
            if limit is not None:
                query += " LIMIT ?"
                args += (limit,)
            cur = await db.execute(query, args)
            rows = await cur.fetchall()
        return [
            {
                "id": row[0],
                "rule_type": row[1],
                "scope": row[2],
                "value": row[3],
                "label": row[4] or "",
                "url": row[5] or "",
                "source_list": row[6] or "manual",
                "created_at": row[7],
            }
            for row in rows
        ]

    async def find_rule_match(
        self,
        video_id: str,
        channel_id: str,
        *,
        preferred_rule_type: str | None = None,
    ) -> Optional[dict[str, str]]:
        async with aiosqlite.connect(self.db_path) as db:
            where_type = ""
            args_prefix: tuple[Any, ...] = ()
            if preferred_rule_type in {"whitelist", "blacklist"}:
                where_type = " AND rule_type = ?"
                args_prefix = (preferred_rule_type,)
            if video_id:
                cur = await db.execute(
                    (
                        "SELECT rule_type, scope, value, source_list FROM rules "
                        f"WHERE scope = 'video' AND value = ?{where_type} ORDER BY id DESC LIMIT 1"
                    ),
                    (video_id, *args_prefix),
                )
                row = await cur.fetchone()
                if row:
                    return {"rule_type": row[0], "scope": row[1], "value": row[2], "source_list": row[3] or "manual"}
            if channel_id:
                cur = await db.execute(
                    (
                        "SELECT rule_type, scope, value, source_list FROM rules "
                        f"WHERE scope = 'channel' AND value = ?{where_type} ORDER BY id DESC LIMIT 1"
                    ),
                    (channel_id, *args_prefix),
                )
                row = await cur.fetchone()
                if row:
                    return {"rule_type": row[0], "scope": row[1], "value": row[2], "source_list": row[3] or "manual"}
        return None

    async def purge_history(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            before_kids = await (await db.execute("SELECT COUNT(*) FROM kids_watch_events")).fetchone()
            await db.execute("DELETE FROM kids_watch_events")
            await db.commit()
        return int(before_kids[0])

    async def db_stats(self) -> dict[str, Any]:
        db_file = Path(self.db_path)
        wal_file = Path(f"{self.db_path}-wal")
        db_size = db_file.stat().st_size if db_file.exists() else 0
        wal_size = wal_file.stat().st_size if wal_file.exists() else 0
        async with aiosqlite.connect(self.db_path) as db:
            kids_watch_events = await (await db.execute("SELECT COUNT(*) FROM kids_watch_events")).fetchone()
            rules_rows = await (await db.execute("SELECT COUNT(*) FROM rules")).fetchone()
            schedules = await (await db.execute("SELECT COUNT(*) FROM schedules")).fetchone()
        return {
            "db_file_bytes": int(db_size),
            "wal_file_bytes": int(wal_size),
            "total_bytes": int(db_size + wal_size),
            "kids_watch_events": int(kids_watch_events[0]),
            "rules": int(rules_rows[0]),
            "schedules": int(schedules[0]),
        }
