from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from .config import get_host_timezone_name


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
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

                CREATE TABLE IF NOT EXISTS devices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT,
                    screen_id TEXT UNIQUE,
                    lounge_token TEXT,
                    auth_state_json TEXT,
                    status TEXT DEFAULT 'offline',
                    last_seen_at TEXT,
                    last_error TEXT
                );

                CREATE TABLE IF NOT EXISTS video_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id INTEGER,
                    video_id TEXT,
                    channel_id TEXT,
                    title TEXT,
                    thumbnail_url TEXT,
                    verdict TEXT,
                    reason TEXT,
                    confidence INTEGER,
                    source TEXT,
                    action_taken TEXT,
                    created_at TEXT
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

                CREATE TABLE IF NOT EXISTS analysis_cache (
                    key TEXT PRIMARY KEY,
                    payload_json TEXT,
                    expires_at TEXT
                );

                CREATE TABLE IF NOT EXISTS sponsorblock_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id INTEGER,
                    video_id TEXT,
                    title TEXT,
                    category TEXT,
                    segment_start REAL,
                    segment_end REAL,
                    action_taken TEXT,
                    status TEXT,
                    error TEXT,
                    created_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_rules_scope_value ON rules(scope, value);
                CREATE INDEX IF NOT EXISTS idx_rules_type_scope ON rules(rule_type, scope);
                CREATE INDEX IF NOT EXISTS idx_schedules_enabled_id ON schedules(enabled, id);
                CREATE INDEX IF NOT EXISTS idx_video_decisions_created ON video_decisions(id DESC);
                CREATE INDEX IF NOT EXISTS idx_video_decisions_verdict ON video_decisions(verdict, id DESC);
                CREATE INDEX IF NOT EXISTS idx_sponsorblock_actions_created ON sponsorblock_actions(id DESC);
                """
            )
            await db.commit()
        await self._migrate_schema()

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
            await db.commit()

    async def _ensure_defaults(self) -> None:
        defaults = {
            "active": "true",
            "schedule_enabled": "true",
            "schedule_start": "07:00",
            "schedule_end": "19:00",
            "timezone": get_host_timezone_name(),
            "custom_prompt": "",
            "failure_webhook_url": "",
            "judge_ok": "true",
            "last_error": "",
            "gemini_api_key_runtime": "",
            "last_failure_alert_at": "",
            "policy_flags_json": "{}",
            "gemini_enabled": "true",
            "sponsorblock_active": "false",
            "sponsorblock_schedule_enabled": "false",
            "sponsorblock_schedule_start": "00:00",
            "sponsorblock_schedule_end": "23:59",
            "sponsorblock_timezone": get_host_timezone_name(),
            "sponsorblock_categories_json": '["sponsor","selfpromo","interaction","intro","outro","music_offtopic"]',
            "sponsorblock_min_length_seconds": "1.0",
            "sponsorblock_release_until": "",
            "mqtt_enabled": "false",
            "mqtt_host": "",
            "mqtt_port": "1883",
            "mqtt_username": "",
            "mqtt_password": "",
            "mqtt_base_topic": "sentinel",
            "mqtt_discovery_prefix": "homeassistant",
            "mqtt_retain": "true",
            "mqtt_tls": "false",
            "mqtt_publish_interval_seconds": "30",
            "mqtt_client_id": "sentinel-yt",
            "blocklist_source_urls": "",
            "allowlist_source_urls": "",
            "allow_policy_flags_json": "{}",
            "schedule_mode": "blocklist",
        }
        for key, value in defaults.items():
            existing = await self.get_setting(key)
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

    async def upsert_device(
        self,
        *,
        name: str,
        screen_id: str,
        lounge_token: str,
        auth_state: dict[str, Any],
        status: str = "paired",
        last_error: str = "",
    ) -> int:
        auth_json = json.dumps(auth_state)
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO devices(name, screen_id, lounge_token, auth_state_json, status, last_seen_at, last_error)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(screen_id) DO UPDATE SET
                    name = excluded.name,
                    lounge_token = excluded.lounge_token,
                    auth_state_json = excluded.auth_state_json,
                    status = excluded.status,
                    last_seen_at = excluded.last_seen_at,
                    last_error = excluded.last_error
                """,
                (name, screen_id, lounge_token, auth_json, status, now, last_error),
            )
            await db.commit()
            cur = await db.execute("SELECT id FROM devices WHERE screen_id = ?", (screen_id,))
            row = await cur.fetchone()
        return int(row[0])

    async def list_devices(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT id, name, screen_id, lounge_token, auth_state_json, status, last_seen_at, last_error
                FROM devices
                ORDER BY id ASC
                """
            )
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "id": row[0],
                    "name": row[1] or "",
                    "screen_id": row[2],
                    "lounge_token": row[3] or "",
                    "auth_state_json": row[4] or "",
                    "status": row[5] or "offline",
                    "last_seen_at": row[6] or "",
                    "last_error": row[7] or "",
                }
            )
        return out

    async def get_device(self, device_id: int) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT id, name, screen_id, lounge_token, auth_state_json, status, last_seen_at, last_error
                FROM devices
                WHERE id = ?
                """,
                (device_id,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "name": row[1] or "",
            "screen_id": row[2],
            "lounge_token": row[3] or "",
            "auth_state_json": row[4] or "",
            "status": row[5] or "offline",
            "last_seen_at": row[6] or "",
            "last_error": row[7] or "",
        }

    async def get_device_by_screen_id(self, screen_id: str) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT id, name, screen_id, lounge_token, auth_state_json, status, last_seen_at, last_error
                FROM devices
                WHERE screen_id = ?
                """,
                (screen_id,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "name": row[1] or "",
            "screen_id": row[2],
            "lounge_token": row[3] or "",
            "auth_state_json": row[4] or "",
            "status": row[5] or "offline",
            "last_seen_at": row[6] or "",
            "last_error": row[7] or "",
        }

    async def update_device_status(self, device_id: int, status: str, error: str = "") -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE devices SET status = ?, last_error = ?, last_seen_at = ? WHERE id = ?",
                (status, error, utc_now_iso(), device_id),
            )
            await db.commit()

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

    async def list_rules(self, *, limit: int = 200, rule_type: str | None = None) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            if rule_type in {"whitelist", "blacklist"}:
                cur = await db.execute(
                    (
                        "SELECT id, rule_type, scope, value, label, url, source_list, created_at "
                        "FROM rules WHERE rule_type = ? ORDER BY id DESC LIMIT ?"
                    ),
                    (rule_type, limit),
                )
            else:
                cur = await db.execute(
                    "SELECT id, rule_type, scope, value, label, url, source_list, created_at FROM rules ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
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

    async def add_video_decision(
        self,
        *,
        device_id: Optional[int],
        video_id: str,
        channel_id: str,
        title: str,
        thumbnail_url: str,
        verdict: str,
        reason: str,
        confidence: int,
        source: str,
        action_taken: str,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO video_decisions(device_id, video_id, channel_id, title, thumbnail_url, verdict, reason, confidence, source, action_taken, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    video_id,
                    channel_id,
                    title,
                    thumbnail_url,
                    verdict,
                    reason,
                    confidence,
                    source,
                    action_taken,
                    utc_now_iso(),
                ),
            )
            await db.commit()

    async def recent_video_decisions(self, limit: int = 200) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT id, device_id, video_id, channel_id, title, thumbnail_url, verdict, reason, confidence, source, action_taken, created_at
                FROM video_decisions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            {
                "id": row[0],
                "device_id": row[1],
                "video_id": row[2],
                "channel_id": row[3],
                "title": row[4],
                "thumbnail_url": row[5],
                "verdict": row[6],
                "reason": row[7],
                "confidence": row[8],
                "source": row[9],
                "action_taken": row[10],
                "created_at": row[11],
            }
            for row in rows
        ]

    async def paged_video_decisions(
        self,
        *,
        page: int,
        page_size: int = 50,
        max_total: int = 500,
    ) -> dict[str, Any]:
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        max_total = max(page_size, int(max_total))
        offset = (page - 1) * page_size
        async with aiosqlite.connect(self.db_path) as db:
            total_row = await (await db.execute("SELECT COUNT(*) FROM video_decisions")).fetchone()
            total_count = min(int(total_row[0]), max_total)
            rows_cur = await db.execute(
                """
                SELECT id, device_id, video_id, channel_id, title, thumbnail_url, verdict, reason, confidence, source, action_taken, created_at
                FROM (
                    SELECT id, device_id, video_id, channel_id, title, thumbnail_url, verdict, reason, confidence, source, action_taken, created_at
                    FROM video_decisions
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (max_total, page_size, offset),
            )
            rows = await rows_cur.fetchall()
        page_count = max(1, (total_count + page_size - 1) // page_size)
        page = min(page, page_count)
        out_rows = [
            {
                "id": row[0],
                "device_id": row[1],
                "video_id": row[2],
                "channel_id": row[3],
                "title": row[4],
                "thumbnail_url": row[5],
                "verdict": row[6],
                "reason": row[7],
                "confidence": row[8],
                "source": row[9],
                "action_taken": row[10],
                "created_at": row[11],
            }
            for row in rows
        ]
        return {
            "rows": out_rows,
            "page": page,
            "page_size": page_size,
            "total_count": total_count,
            "page_count": page_count,
            "has_prev": page > 1,
            "has_next": page < page_count,
        }

    async def recent_blocked_decisions(self, limit: int = 10) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT id, device_id, video_id, channel_id, title, verdict, source, action_taken, created_at
                FROM video_decisions
                WHERE verdict = 'BLOCK'
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            {
                "id": row[0],
                "device_id": row[1],
                "video_id": row[2],
                "channel_id": row[3],
                "title": row[4],
                "verdict": row[5],
                "source": row[6],
                "action_taken": row[7],
                "created_at": row[8],
            }
            for row in rows
        ]

    async def recent_allowed_decisions(self, limit: int = 10) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT id, device_id, video_id, channel_id, title, verdict, source, action_taken, created_at
                FROM video_decisions
                WHERE verdict = 'ALLOW'
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            {
                "id": row[0],
                "device_id": row[1],
                "video_id": row[2],
                "channel_id": row[3],
                "title": row[4],
                "verdict": row[5],
                "source": row[6],
                "action_taken": row[7],
                "created_at": row[8],
            }
            for row in rows
        ]

    async def cache_set(self, key: str, payload: dict[str, Any], expires_at: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO analysis_cache(key, payload_json, expires_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET payload_json = excluded.payload_json, expires_at = excluded.expires_at
                """,
                (key, json.dumps(payload), expires_at),
            )
            await db.commit()

    async def cache_get(self, key: str) -> Optional[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT payload_json, expires_at FROM analysis_cache WHERE key = ?",
                (key,),
            )
            row = await cur.fetchone()
        if not row:
            return None
        payload, expires_at = row
        if expires_at and expires_at < utc_now_iso():
            return None
        return json.loads(payload)

    async def purge_analysis_cache(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            before = await (await db.execute("SELECT COUNT(*) FROM analysis_cache")).fetchone()
            await db.execute("DELETE FROM analysis_cache")
            await db.commit()
        return int(before[0])

    async def purge_history(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            before = await (await db.execute("SELECT COUNT(*) FROM video_decisions")).fetchone()
            await db.execute("DELETE FROM video_decisions")
            await db.commit()
        return int(before[0])

    async def add_sponsorblock_action(
        self,
        *,
        device_id: int,
        video_id: str,
        title: str,
        category: str,
        segment_start: float,
        segment_end: float,
        action_taken: str,
        status: str,
        error: str = "",
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO sponsorblock_actions(
                    device_id, video_id, title, category, segment_start, segment_end, action_taken, status, error, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    device_id,
                    video_id,
                    title,
                    category,
                    segment_start,
                    segment_end,
                    action_taken,
                    status,
                    error,
                    utc_now_iso(),
                ),
            )
            await db.commit()

    async def recent_sponsorblock_actions(self, limit: int = 20) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT id, device_id, video_id, title, category, segment_start, segment_end, action_taken, status, error, created_at
                FROM sponsorblock_actions
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cur.fetchall()
        return [
            {
                "id": row[0],
                "device_id": row[1],
                "video_id": row[2],
                "title": row[3],
                "category": row[4],
                "segment_start": row[5],
                "segment_end": row[6],
                "action_taken": row[7],
                "status": row[8],
                "error": row[9],
                "created_at": row[10],
            }
            for row in rows
        ]

    async def db_stats(self) -> dict[str, Any]:
        db_file = Path(self.db_path)
        wal_file = Path(f"{self.db_path}-wal")
        db_size = db_file.stat().st_size if db_file.exists() else 0
        wal_size = wal_file.stat().st_size if wal_file.exists() else 0
        async with aiosqlite.connect(self.db_path) as db:
            decisions = await (await db.execute("SELECT COUNT(*) FROM video_decisions")).fetchone()
            cache_rows = await (await db.execute("SELECT COUNT(*) FROM analysis_cache")).fetchone()
            rules_rows = await (await db.execute("SELECT COUNT(*) FROM rules")).fetchone()
            sb_rows = await (await db.execute("SELECT COUNT(*) FROM sponsorblock_actions")).fetchone()
            schedules = await (await db.execute("SELECT COUNT(*) FROM schedules")).fetchone()
        return {
            "db_file_bytes": int(db_size),
            "wal_file_bytes": int(wal_size),
            "total_bytes": int(db_size + wal_size),
            "video_decisions": int(decisions[0]),
            "analysis_cache": int(cache_rows[0]),
            "rules": int(rules_rows[0]),
            "sponsorblock_actions": int(sb_rows[0]),
            "schedules": int(schedules[0]),
        }

    async def home_dashboard_stats(self, *, days: int = 7) -> dict[str, Any]:
        days = max(3, min(30, int(days)))
        since_dt = datetime.now(timezone.utc) - timedelta(days=days - 1)
        since_iso = since_dt.isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            totals_row = await (
                await db.execute(
                    """
                    SELECT
                        COUNT(*) AS total_count,
                        SUM(CASE WHEN verdict = 'ALLOW' THEN 1 ELSE 0 END) AS allow_count,
                        SUM(CASE WHEN verdict = 'BLOCK' THEN 1 ELSE 0 END) AS block_count,
                        COUNT(DISTINCT CASE WHEN TRIM(COALESCE(video_id, '')) <> '' THEN video_id END) AS unique_videos,
                        COUNT(DISTINCT CASE WHEN TRIM(COALESCE(channel_id, '')) <> '' THEN channel_id END) AS unique_channels
                    FROM video_decisions
                    """
                )
            ).fetchone()

            source_rows = await (
                await db.execute(
                    """
                    SELECT
                        COALESCE(NULLIF(source, ''), 'unknown') AS source,
                        SUM(CASE WHEN verdict = 'ALLOW' THEN 1 ELSE 0 END) AS allow_count,
                        SUM(CASE WHEN verdict = 'BLOCK' THEN 1 ELSE 0 END) AS block_count
                    FROM video_decisions
                    GROUP BY COALESCE(NULLIF(source, ''), 'unknown')
                    ORDER BY (allow_count + block_count) DESC
                    LIMIT 8
                    """
                )
            ).fetchall()

            trend_rows = await (
                await db.execute(
                    """
                    SELECT
                        SUBSTR(created_at, 1, 10) AS day,
                        SUM(CASE WHEN verdict = 'ALLOW' THEN 1 ELSE 0 END) AS allow_count,
                        SUM(CASE WHEN verdict = 'BLOCK' THEN 1 ELSE 0 END) AS block_count
                    FROM video_decisions
                    WHERE created_at >= ?
                    GROUP BY SUBSTR(created_at, 1, 10)
                    ORDER BY day ASC
                    """,
                    (since_iso,),
                )
            ).fetchall()

            top_block_rows = await (
                await db.execute(
                    """
                    SELECT
                        COALESCE(NULLIF(video_id, ''), '-') AS video_id,
                        COALESCE(NULLIF(title, ''), COALESCE(NULLIF(video_id, ''), 'Unknown title')) AS title,
                        COUNT(*) AS block_count
                    FROM video_decisions
                    WHERE verdict = 'BLOCK'
                    GROUP BY COALESCE(NULLIF(video_id, ''), '-'), COALESCE(NULLIF(title, ''), COALESCE(NULLIF(video_id, ''), 'Unknown title'))
                    ORDER BY block_count DESC, title ASC
                    LIMIT 5
                    """
                )
            ).fetchall()

            rule_rows = await (await db.execute("SELECT rule_type, COUNT(*) FROM rules GROUP BY rule_type")).fetchall()
            sb_rows = await (
                await db.execute(
                    """
                    SELECT
                        COUNT(*) AS total_actions,
                        SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok_actions
                    FROM sponsorblock_actions
                    """
                )
            ).fetchone()

        total_count = int((totals_row[0] or 0) if totals_row else 0)
        allow_count = int((totals_row[1] or 0) if totals_row else 0)
        block_count = int((totals_row[2] or 0) if totals_row else 0)
        unique_videos = int((totals_row[3] or 0) if totals_row else 0)
        unique_channels = int((totals_row[4] or 0) if totals_row else 0)
        block_rate = round((block_count / total_count) * 100.0, 1) if total_count else 0.0

        source_breakdown = [
            {
                "source": str(row[0] or "unknown"),
                "allow_count": int(row[1] or 0),
                "block_count": int(row[2] or 0),
                "total": int((row[1] or 0) + (row[2] or 0)),
            }
            for row in source_rows
        ]

        trend_map = {
            str(row[0]): {"allow_count": int(row[1] or 0), "block_count": int(row[2] or 0)}
            for row in trend_rows
            if row and row[0]
        }
        trend: list[dict[str, Any]] = []
        now = datetime.now(timezone.utc)
        for idx in range(days):
            day = (now - timedelta(days=(days - 1 - idx))).date().isoformat()
            entry = trend_map.get(day, {"allow_count": 0, "block_count": 0})
            trend.append(
                {
                    "day": day,
                    "allow_count": int(entry["allow_count"]),
                    "block_count": int(entry["block_count"]),
                    "total": int(entry["allow_count"] + entry["block_count"]),
                }
            )

        top_blocked = [
            {
                "video_id": str(row[0] or "-"),
                "title": str(row[1] or "Unknown title"),
                "block_count": int(row[2] or 0),
                "url": f"https://www.youtube.com/watch?v={row[0]}" if row[0] and row[0] != "-" else "",
            }
            for row in top_block_rows
        ]

        rule_counts = {"blacklist": 0, "whitelist": 0}
        for row in rule_rows:
            key = str(row[0] or "").strip().lower()
            if key in rule_counts:
                rule_counts[key] = int(row[1] or 0)

        sponsorblock_total = int((sb_rows[0] or 0) if sb_rows else 0)
        sponsorblock_ok = int((sb_rows[1] or 0) if sb_rows else 0)

        return {
            "totals": {
                "total_count": total_count,
                "allow_count": allow_count,
                "block_count": block_count,
                "block_rate_percent": block_rate,
                "unique_videos": unique_videos,
                "unique_channels": unique_channels,
                "sponsorblock_total": sponsorblock_total,
                "sponsorblock_ok": sponsorblock_ok,
                "rule_blacklist_count": int(rule_counts["blacklist"]),
                "rule_whitelist_count": int(rule_counts["whitelist"]),
            },
            "source_breakdown": source_breakdown,
            "trend": trend,
            "top_blocked": top_blocked,
        }

    async def counts(self) -> dict[str, int]:
        async with aiosqlite.connect(self.db_path) as db:
            total = await (await db.execute("SELECT COUNT(*) FROM devices")).fetchone()
            connected = await (
                await db.execute("SELECT COUNT(*) FROM devices WHERE status IN ('connected', 'linked')")
            ).fetchone()
        return {
            "devices_total": int(total[0]),
            "devices_connected": int(connected[0]),
        }
