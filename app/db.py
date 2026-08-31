from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import aiosqlite

from .config import DEFAULT_POLICY_FLAGS, get_host_timezone_name


KIDS_HOME_SOURCE_REFERENCE = "__youtube_kids_home__"


def _source_channel_id(kind: Any, reference: Any) -> str | None:
    if str(kind or "") != "channel":
        return None
    raw = str(reference or "").strip()
    if not raw or raw == KIDS_HOME_SOURCE_REFERENCE:
        return None
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        parts = [part for part in parsed.path.split("/") if part]
        return parts[1] if len(parts) == 2 and parts[0] == "channel" else None
    return raw


def _catalog_identity_is_known(item: dict[str, Any], source: dict[str, Any]) -> bool:
    channel_id = str(item.get("channel_id") or "").strip()
    channel_title = str(item.get("channel_title") or "").strip()
    if not channel_title:
        return False
    expected_channel_id = _source_channel_id(source.get("kind"), source.get("reference"))
    if expected_channel_id is None:
        return True
    if not channel_id:
        return False
    return expected_channel_id is None or channel_id == expected_channel_id


def _catalog_item_is_authorized(item: dict[str, Any], source: dict[str, Any]) -> bool:
    if (
        item.get("state") != "approved"
        or source.get("state") != "approved"
        or source.get("safety_verdict") != "SAFE"
        or source.get("reference") == KIDS_HOME_SOURCE_REFERENCE
        or not _catalog_identity_is_known(item, source)
    ):
        return False
    return True


def _quality_height_or_default(value: Any) -> int:
    return value if type(value) is int and 720 <= value <= 1080 else 720


def _stored_candidate_meets_policy(
    candidate_json: Any,
    quality_height: Any,
    minimum_quality_height: int,
) -> bool:
    if (
        type(quality_height) is not int
        or not minimum_quality_height <= quality_height <= 1080
    ):
        return False
    try:
        candidate = json.loads(str(candidate_json))
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(candidate, dict) or candidate.get("kind") not in (None, "adaptive_mpv"):
        return False
    media_url = candidate.get("media_url")
    audio_url = candidate.get("audio_url")
    return (
        type(candidate.get("quality_height")) is int
        and candidate["quality_height"] == quality_height
        and isinstance(media_url, str)
        and bool(media_url)
        and isinstance(audio_url, str)
        and bool(audio_url)
        and media_url != audio_url
    )


def _catalog_row_context(
    row: tuple[Any, ...],
    columns: list[str],
) -> tuple[dict[str, Any], dict[str, Any], Any]:
    values = dict(zip(columns, row))
    source = {
        "kind": values.pop("_source_kind", None),
        "reference": values.pop("_source_reference", None),
        "title": values.pop("_source_title", None),
        "state": values.pop("_source_state", None),
        "safety_verdict": values.pop("_source_safety_verdict", None),
    }
    candidate_json = values.pop("_candidate_json", None)
    return values, source, candidate_json


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

                CREATE TABLE IF NOT EXISTS catalog_meta (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS catalog_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('channel', 'playlist')),
                    reference TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL DEFAULT '',
                    safety_verdict TEXT NOT NULL DEFAULT 'UNCERTAIN',
                    safety_reason TEXT NOT NULL DEFAULT '',
                    safety_checked_at TEXT,
                    safety_policy_version TEXT NOT NULL DEFAULT '',
                    safety_evidence_json TEXT NOT NULL DEFAULT '[]',
                    safety_sample_count INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'candidate'
                        CHECK(state IN ('candidate', 'approved', 'blocked', 'revoked', 'unknown')),
                    actor TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    correlation_id TEXT NOT NULL
                );
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
            cur = await db.execute("PRAGMA table_info(catalog_sources)")
            source_cols = {row[1] for row in await cur.fetchall()}
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
            await db.commit()

    async def _catalog_revision(self, db: aiosqlite.Connection) -> int:
        await db.execute("INSERT OR IGNORE INTO catalog_meta(key, value) VALUES ('revision', 0)")
        row = await (await db.execute("SELECT value FROM catalog_meta WHERE key='revision'")).fetchone()
        revision = int(row[0]) + 1
        await db.execute("UPDATE catalog_meta SET value=? WHERE key='revision'", (revision,))
        return revision

    async def catalog_revision(self) -> int:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute("SELECT value FROM catalog_meta WHERE key='revision'")).fetchone()
        return int(row[0]) if row else 0

    async def catalog_create(self, entity: str, values: dict[str, Any]) -> dict[str, Any]:
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            revision = await self._catalog_revision(db)
            if entity == "source":
                cur = await db.execute(
                    "INSERT INTO catalog_sources(kind,reference,title,actor,changed_at,reason,revision,correlation_id) VALUES(?,?,?,?,?,?,?,?)",
                    (values["kind"], values["reference"].strip(), values.get("title", "").strip(), "system",
                     now, "candidate created", revision, values["correlation_id"]),
                )
            else:
                cur = await db.execute(
                    """
                    INSERT INTO catalog_items(
                        video_id,title,source_id,channel_id,channel_title,thumbnail_url,
                        duration_seconds,visual_category,actor,changed_at,reason,revision,correlation_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (values["video_id"].strip(), values.get("title", "").strip(), values.get("source_id"),
                     str(values.get("channel_id", "") or "").strip()[:128],
                     str(values.get("channel_title", "") or "").strip()[:500],
                     values.get("thumbnail_url", "").strip(), int(values.get("duration_seconds", 0) or 0),
                     values.get("visual_category", "general").strip() or "general",
                     "system", now, "candidate created", revision, values["correlation_id"]),
                )
            entity_id = cur.lastrowid
            await db.execute(
                """
                INSERT INTO kids_audit_events(event, entity_type, entity_id, actor, reason, revision, correlation_id, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("candidate_created", entity, entity_id, "system", "candidate created", revision, values["correlation_id"], now),
            )
            await db.commit()
        await self.kids_resolve_sync_backlog()
        return await self.catalog_get(entity, entity_id)

    async def catalog_get(self, entity: str, entity_id: int) -> dict[str, Any] | None:
        table = "catalog_sources" if entity == "source" else "catalog_items"
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(f"SELECT * FROM {table} WHERE id=?", (entity_id,))
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description] if row else []
        return dict(zip(cols, row)) if row else None

    async def catalog_transition(
        self,
        entity: str,
        entity_id: int,
        values: dict[str, Any],
        *,
        expected_state: str | None = None,
    ) -> dict[str, Any] | None:
        table = "catalog_sources" if entity == "source" else "catalog_items"
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute(f"SELECT state FROM {table} WHERE id=?", (entity_id,))).fetchone()
            if not row:
                await db.rollback()
                return None
            old = row[0]
            if expected_state is not None and old != expected_state:
                await db.rollback()
                return None
            if old == "revoked" and values["state"] == "approved":
                await db.rollback()
                raise ValueError("revoked entries cannot be approved")
            revision = await self._catalog_revision(db)
            await db.execute(
                f"UPDATE {table} SET state=?, actor=?, changed_at=?, reason=?, revision=?, correlation_id=? WHERE id=?",
                (values["state"], values["actor"], now, values["reason"], revision, values["correlation_id"], entity_id),
            )
            await db.execute(
                "INSERT INTO catalog_transitions(entity_type,entity_id,from_state,to_state,actor,changed_at,reason,revision,correlation_id) VALUES(?,?,?,?,?,?,?,?,?)",
                (entity, entity_id, old, values["state"], values["actor"], now, values["reason"], revision, values["correlation_id"]),
            )
            await db.execute(
                """
                INSERT INTO kids_audit_events(event, entity_type, entity_id, actor, reason, revision, correlation_id, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("state_changed", entity, entity_id, values["actor"], values["reason"], revision, values["correlation_id"], now),
            )
            await db.commit()
        await self.kids_resolve_sync_backlog()
        return await self.catalog_get(entity, entity_id)

    async def catalog_blocklist_restore_candidates(self) -> list[dict[str, Any]]:
        """Return rows whose latest blocklist transition can be safely reversed."""
        actors = ("kids-guardian-blocklist", "kids-guardian-policy")
        result: list[dict[str, Any]] = []
        async with aiosqlite.connect(self.db_path) as db:
            for entity_type, table in (("source", "catalog_sources"), ("item", "catalog_items")):
                cur = await db.execute(
                    f"SELECT id FROM {table} WHERE state='blocked' AND actor IN (?, ?)",
                    actors,
                )
                for (entity_id,) in await cur.fetchall():
                    transition = await (
                        await db.execute(
                            """
                            SELECT from_state,to_state,actor
                            FROM catalog_transitions
                            WHERE entity_type=? AND entity_id=?
                            ORDER BY id DESC LIMIT 1
                            """,
                            (entity_type, entity_id),
                        )
                    ).fetchone()
                    if (
                        transition
                        and transition[1] == "blocked"
                        and transition[2] in actors
                        and transition[0] in {"candidate", "approved", "unknown"}
                    ):
                        result.append(
                            {
                                "entity_type": entity_type,
                                "entity_id": int(entity_id),
                                "previous_state": transition[0],
                            }
                        )
        return result

    async def catalog_source_safety_update(
        self,
        source_id: int,
        *,
        verdict: str,
        reason: str,
        actor: str,
        correlation_id: str,
        policy_version: str = "",
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        verdict = verdict.strip().upper()
        if verdict not in {"SAFE", "UNSAFE", "UNCERTAIN"}:
            raise ValueError("invalid source safety verdict")
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute("SELECT id FROM catalog_sources WHERE id=?", (source_id,))
            ).fetchone()
            if not row:
                return None
            revision = await self._catalog_revision(db)
            await db.execute(
                """
                UPDATE catalog_sources
                SET safety_verdict=?, safety_reason=?, safety_checked_at=?,
                    safety_policy_version=?, safety_evidence_json=?, safety_sample_count=?,
                    actor=?, changed_at=?, reason=?, revision=?, correlation_id=?
                WHERE id=?
                """,
                (
                    verdict,
                    reason[:1000],
                    now,
                    policy_version[:128],
                    json.dumps((evidence or [])[:20], separators=(",", ":"), ensure_ascii=True),
                    min(len(evidence or []), 20),
                    actor,
                    now,
                    reason[:1000],
                    revision,
                    correlation_id,
                    source_id,
                ),
            )
            await db.execute(
                """
                INSERT INTO kids_audit_events(
                    event, entity_type, entity_id, actor, reason, revision, correlation_id, created_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "source_safety_changed",
                    "source",
                    source_id,
                    actor,
                    f"{verdict}: {reason[:1000]}",
                    revision,
                    correlation_id,
                    now,
                ),
            )
            await db.commit()
        await self.kids_resolve_sync_backlog()
        return await self.catalog_get("source", source_id)

    async def catalog_item_refresh(
        self,
        item_id: int,
        *,
        title: str,
        source_id: int,
        thumbnail_url: str,
        duration_seconds: int,
        visual_category: str,
        correlation_id: str,
        sync_backlog: bool = True,
        channel_id: str = "",
        channel_title: str = "",
    ) -> dict[str, Any] | None:
        values = (
            title.strip()[:500],
            source_id,
            channel_id.strip()[:128],
            channel_title.strip()[:500],
            thumbnail_url.strip()[:2000],
            max(0, int(duration_seconds)),
            visual_category.strip()[:64] or "general",
        )
        changed = False
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT title,source_id,channel_id,channel_title,thumbnail_url,
                           duration_seconds,visual_category
                    FROM catalog_items WHERE id=?
                    """,
                    (item_id,),
                )
            ).fetchone()
            if not row:
                return None
            changed = tuple(row) != values
            if changed:
                now = utc_now_iso()
                revision = await self._catalog_revision(db)
                await db.execute(
                    """
                    UPDATE catalog_items
                    SET title=?,source_id=?,channel_id=?,channel_title=?,thumbnail_url=?,
                        duration_seconds=?,visual_category=?,
                        actor='kids-ingest',changed_at=?,reason='metadata refreshed',
                        revision=?,correlation_id=?
                    WHERE id=?
                    """,
                    (*values, now, revision, correlation_id, item_id),
                )
                await db.execute(
                    """
                    INSERT INTO kids_audit_events(
                        event,entity_type,entity_id,actor,reason,revision,correlation_id,created_at
                    ) VALUES('item_metadata_refreshed','item',?,'kids-ingest','metadata refreshed',?,?,?)
                    """,
                    (item_id, revision, correlation_id, now),
                )
                await db.commit()
        if changed and sync_backlog:
            await self.kids_resolve_sync_backlog()
        return await self.catalog_get("item", item_id)

    async def catalog_approved_items_for_policy(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT i.*,COALESCE(s.title,'') AS source_title,
                       COALESCE(s.reference,'') AS source_reference
                FROM catalog_items i
                LEFT JOIN catalog_sources s ON s.id=i.source_id
                WHERE i.state='approved'
                ORDER BY i.id ASC
                """
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]

    async def catalog_items_list(self, minimum_remaining_seconds: int = 300) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT i.*,s.kind AS _source_kind,s.reference AS _source_reference,
                       s.title AS _source_title,s.state AS _source_state,
                       s.safety_verdict AS _source_safety_verdict
                FROM catalog_items i JOIN catalog_sources s ON s.id=i.source_id
                WHERE i.state='approved' AND s.state='approved'
                  AND s.safety_verdict='SAFE' AND s.reference!=?
                ORDER BY i.id ASC
                """,
                (KIDS_HOME_SOURCE_REFERENCE,),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        result: list[dict[str, Any]] = []
        for row in rows:
            item, source, _candidate_json = _catalog_row_context(row, cols)
            if _catalog_item_is_authorized(item, source):
                result.append(item)
        return result

    async def kids_eligible_feed_list(
        self,
        minimum_remaining_seconds: int,
        minimum_quality_height: int = 720,
    ) -> list[dict[str, Any]]:
        minimum_quality_height = _quality_height_or_default(minimum_quality_height)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT i.*,s.kind AS _source_kind,s.reference AS _source_reference,
                       s.title AS _source_title,s.state AS _source_state,
                       s.safety_verdict AS _source_safety_verdict,
                       b.candidate_json AS _candidate_json,b.quality_height AS _backlog_quality_height
                FROM catalog_items i JOIN catalog_sources s ON s.id=i.source_id
                JOIN kids_resolve_backlog b ON b.item_id=i.id
                WHERE i.state='approved' AND s.state='approved' AND s.safety_verdict='SAFE'
                  AND s.reference!=? AND b.status='ready' AND b.quality_height>=? AND b.expires_at>?
                ORDER BY b.resolved_at DESC,i.id ASC
                """,
                (
                    KIDS_HOME_SOURCE_REFERENCE,
                    minimum_quality_height,
                    (datetime.now(timezone.utc) + timedelta(seconds=max(0, minimum_remaining_seconds))).isoformat(),
                ),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        result: list[dict[str, Any]] = []
        for row in rows:
            item, source, candidate_json = _catalog_row_context(row, cols)
            quality_height = item.pop("_backlog_quality_height", None)
            if (
                _catalog_item_is_authorized(item, source)
                and _stored_candidate_meets_policy(
                    candidate_json,
                    quality_height,
                    minimum_quality_height,
                )
            ):
                result.append(item)
        return result

    async def kids_resolve_sync_backlog(self, *, minimum_quality_height: int = 720) -> None:
        """Make eligibility changes immediately remove technical playback authority."""
        minimum_quality_height = _quality_height_or_default(minimum_quality_height)
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO kids_resolve_backlog(item_id,video_id,status,updated_at)
                SELECT i.id,i.video_id,'pending',?
                FROM catalog_items i JOIN catalog_sources s ON s.id=i.source_id
                WHERE i.state='approved' AND s.state='approved' AND s.safety_verdict='SAFE'
                  AND s.reference!=?
                ON CONFLICT(item_id) DO UPDATE SET
                    video_id=excluded.video_id,
                    status=CASE WHEN kids_resolve_backlog.status='blocked' THEN 'pending'
                                ELSE kids_resolve_backlog.status END,
                    updated_at=CASE WHEN kids_resolve_backlog.status='blocked'
                                    THEN excluded.updated_at ELSE kids_resolve_backlog.updated_at END
                """,
                (now, KIDS_HOME_SOURCE_REFERENCE),
            )
            cur = await db.execute(
                """
                SELECT b.item_id,b.status,b.candidate_json,b.quality_height AS _backlog_quality_height,
                       i.*,s.kind AS _source_kind,s.reference AS _source_reference,
                       s.title AS _source_title,s.state AS _source_state,
                       s.safety_verdict AS _source_safety_verdict
                FROM kids_resolve_backlog b
                LEFT JOIN catalog_items i ON i.id=b.item_id
                LEFT JOIN catalog_sources s ON s.id=i.source_id
                """,
            )
            rows = await cur.fetchall()
            columns = [d[0] for d in cur.description]
            for row in rows:
                values = dict(zip(columns, row))
                item_id = values["item_id"]
                status = values["status"]
                candidate_json = values["_candidate_json"] if "_candidate_json" in values else values["candidate_json"]
                quality_height = values["_backlog_quality_height"]
                item = {key: value for key, value in values.items() if key not in {
                    "item_id", "status", "candidate_json", "_candidate_json",
                    "_backlog_quality_height",
                }}
                source = {
                    "kind": values.get("_source_kind"),
                    "reference": values.get("_source_reference"),
                    "title": values.get("_source_title"),
                    "state": values.get("_source_state"),
                    "safety_verdict": values.get("_source_safety_verdict"),
                }
                if not _catalog_item_is_authorized(item, source):
                    await db.execute(
                        """
                        UPDATE kids_resolve_backlog
                        SET status='blocked',candidate_json='',quality_height=NULL,codec='',
                            resolved_at=NULL,expires_at=NULL,next_attempt_at=NULL,
                            last_error_code='ineligible',updated_at=?
                        WHERE item_id=?
                        """,
                        (now, item_id),
                    )
                elif status == "ready" and not _stored_candidate_meets_policy(
                    candidate_json,
                    quality_height,
                    minimum_quality_height,
                ):
                    await db.execute(
                        """
                        UPDATE kids_resolve_backlog
                        SET status='pending',candidate_json='',quality_height=NULL,codec='',
                            resolved_at=NULL,expires_at=NULL,next_attempt_at=NULL,
                            last_error_code='quality_below_policy',updated_at=?
                        WHERE item_id=?
                        """,
                        (now, item_id),
                    )
            await db.execute(
                """
                UPDATE kids_resolve_backlog
                SET status='retry', next_attempt_at=?, last_error_code='stale_running', updated_at=?
                WHERE status='running' AND updated_at < ?
                """,
                (now, now, (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()),
            )
            await db.commit()

    async def kids_resolve_claim_due(self, *, limit: int, refresh_margin_seconds: int) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 20))
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        refresh_at = (now + timedelta(seconds=max(0, refresh_margin_seconds))).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                """
                SELECT item_id,video_id FROM (
                    SELECT item_id,video_id,1 AS priority,COALESCE(next_attempt_at,'') AS due
                    FROM kids_resolve_backlog
                    WHERE status IN ('pending','retry')
                      AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                    UNION ALL
                    SELECT item_id,video_id,0 AS priority,expires_at AS due
                    FROM kids_resolve_backlog
                    WHERE status='ready' AND expires_at<=?
                )
                ORDER BY priority ASC,due ASC,item_id ASC LIMIT ?
                """,
                (now_iso, refresh_at, bounded),
            )
            rows = await cur.fetchall()
            for item_id, _video_id in rows:
                await db.execute(
                    """
                    UPDATE kids_resolve_backlog
                    SET status=CASE WHEN status='ready' THEN 'ready' ELSE 'running' END,
                        updated_at=?
                    WHERE item_id=?
                    """,
                    (now_iso, item_id),
                )
            await db.commit()
        return [{"item_id": int(item_id), "video_id": str(video_id)} for item_id, video_id in rows]

    async def kids_resolve_success(
        self,
        *,
        item_id: int,
        candidate: dict[str, Any],
        quality_height: int,
        codec: str,
        resolved_at: str,
        expires_at: str,
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE kids_resolve_backlog
                SET status='ready', candidate_json=?, quality_height=?, codec=?, resolved_at=?, expires_at=?,
                    next_attempt_at=NULL, last_error_code='', updated_at=?
                WHERE item_id=?
                """,
                (
                    json.dumps(candidate, separators=(",", ":"), ensure_ascii=True),
                    quality_height,
                    codec,
                    resolved_at,
                    expires_at,
                    utc_now_iso(),
                    item_id,
                ),
            )
            await db.commit()

    async def kids_resolve_failure(self, *, item_id: int, reason_code: str) -> None:
        safe_code = reason_code if reason_code in {
            "backend_unavailable", "no_compatible_stream", "invalid_candidate", "resolver_error", "stale_running"
        } else "resolver_error"
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute("SELECT attempt_count FROM kids_resolve_backlog WHERE item_id=?", (item_id,))
            ).fetchone()
            attempts = min(10, int(row[0] if row else 0) + 1)
            delay = min(3600, 30 * (2 ** min(attempts - 1, 6)))
            await db.execute(
                """
                UPDATE kids_resolve_backlog
                SET status='retry', candidate_json='', quality_height=NULL, codec='', resolved_at=NULL, expires_at=NULL,
                    attempt_count=?, next_attempt_at=?, last_error_code=?, updated_at=?
                WHERE item_id=?
                """,
                (attempts, (now + timedelta(seconds=delay)).isoformat(), safe_code, now.isoformat(), item_id),
            )
            await db.commit()

    async def kids_resolve_summary(self, *, minimum_remaining_seconds: int = 300) -> dict[str, Any]:
        now = (datetime.now(timezone.utc) + timedelta(seconds=max(0, minimum_remaining_seconds))).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                "SELECT status,COUNT(*) FROM kids_resolve_backlog GROUP BY status"
            )
            counts = {str(status): int(count) for status, count in await cur.fetchall()}
            row = await (
                await db.execute(
                    "SELECT COUNT(*) FROM kids_resolve_backlog WHERE status='ready' AND expires_at>?",
                    (now,),
                )
            ).fetchone()
        return {"counts": counts, "fresh_ready": int(row[0] if row else 0)}

    async def kids_resolve_recent_rows(self, limit: int = 20) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 100))
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT b.item_id,b.video_id,i.title,b.status,b.quality_height,b.codec,b.expires_at,
                       b.attempt_count,b.last_error_code,b.updated_at
                FROM kids_resolve_backlog b JOIN catalog_items i ON i.id=b.item_id
                ORDER BY b.updated_at DESC LIMIT ?
                """,
                (bounded,),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]

    async def kids_playback_authorization(
        self,
        video_id: str,
        *,
        minimum_remaining_seconds: int,
        minimum_quality_height: int = 720,
    ) -> dict[str, Any] | None:
        minimum_quality_height = _quality_height_or_default(minimum_quality_height)
        expires_after = (datetime.now(timezone.utc) + timedelta(seconds=max(0, minimum_remaining_seconds))).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT i.id AS item_id,i.video_id,i.title,i.channel_id,i.channel_title,
                       b.candidate_json,b.expires_at,b.quality_height,b.codec,
                       s.kind AS _source_kind,s.reference AS _source_reference,
                       s.title AS _source_title,s.state AS _source_state,
                       s.safety_verdict AS _source_safety_verdict
                FROM catalog_items i JOIN catalog_sources s ON s.id=i.source_id
                JOIN kids_resolve_backlog b ON b.item_id=i.id
                WHERE i.video_id=? AND i.state='approved' AND s.state='approved'
                  AND s.safety_verdict='SAFE' AND s.reference!=?
                  AND b.status='ready' AND b.quality_height>=? AND b.expires_at>?
                """,
                (video_id, KIDS_HOME_SOURCE_REFERENCE, minimum_quality_height, expires_after),
            )
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description] if row else []
        if not row:
            return None
        result = dict(zip(cols, row))
        source = {
            "kind": result.pop("_source_kind", None),
            "reference": result.pop("_source_reference", None),
            "title": result.pop("_source_title", None),
            "state": result.pop("_source_state", None),
            "safety_verdict": result.pop("_source_safety_verdict", None),
        }
        item = {
            "state": "approved",
            "video_id": result["video_id"],
            "title": result["title"],
            "channel_id": result["channel_id"],
            "channel_title": result["channel_title"],
        }
        candidate_json = result["candidate_json"]
        quality_height = result["quality_height"]
        if (
            not _catalog_item_is_authorized(item, source)
            or not _stored_candidate_meets_policy(
                candidate_json,
                quality_height,
                minimum_quality_height,
            )
        ):
            return None
        try:
            candidate = json.loads(str(result.pop("candidate_json")))
        except (TypeError, json.JSONDecodeError):
            return None
        result["candidate"] = candidate
        return result

    async def kids_playback_policy_authorization(self, video_id: str) -> dict[str, Any] | None:
        """Revalidate an active relay without coupling it to the next resolve candidate."""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT i.id AS item_id,i.video_id,i.title,i.channel_id,i.channel_title,
                       s.kind AS _source_kind,s.reference AS _source_reference,
                       s.title AS _source_title,s.state AS _source_state,
                       s.safety_verdict AS _source_safety_verdict
                FROM catalog_items i JOIN catalog_sources s ON s.id=i.source_id
                WHERE i.video_id=? AND i.state='approved' AND s.state='approved'
                  AND s.safety_verdict='SAFE' AND s.reference!=?
                """,
                (video_id, KIDS_HOME_SOURCE_REFERENCE),
            )
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description] if row else []
        if not row:
            return None
        result = dict(zip(cols, row))
        source = {
            "kind": result.pop("_source_kind", None),
            "reference": result.pop("_source_reference", None),
            "title": result.pop("_source_title", None),
            "state": result.pop("_source_state", None),
            "safety_verdict": result.pop("_source_safety_verdict", None),
        }
        item = {
            "state": "approved",
            "video_id": result["video_id"],
            "title": result["title"],
            "channel_id": result["channel_id"],
            "channel_title": result["channel_title"],
        }
        if not _catalog_item_is_authorized(item, source):
            return None
        return {"item_id": result["item_id"], "video_id": result["video_id"]}

    async def catalog_item_list_all(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT * FROM catalog_items ORDER BY id ASC")
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]

    async def catalog_item_by_video(self, video_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT * FROM catalog_items WHERE video_id=?", (video_id,))
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description] if row else []
        return dict(zip(cols, row)) if row else None

    async def catalog_sources_list(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT * FROM catalog_sources ORDER BY id ASC")
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]

    async def kids_kill_switch_enabled(self) -> bool:
        value = await self.get_setting("kids_kill_switch")
        return (value or "false").strip().lower() == "true"

    async def set_kids_kill_switch(
        self,
        *,
        enabled: bool,
        actor: str,
        reason: str,
        correlation_id: str,
    ) -> bool:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO settings(key, value) VALUES('kids_kill_switch', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("true" if enabled else "false",),
            )
            await db.execute(
                """
                INSERT INTO kids_audit_events(
                    event, entity_type, entity_id, actor, reason, revision, correlation_id, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("kill_switch_changed", "control", None, actor, reason, 0, correlation_id, utc_now_iso()),
            )
            await db.commit()
        return await self.kids_kill_switch_enabled() == enabled

    async def audit_kids_event(
        self,
        *,
        event: str,
        actor: str = "",
        reason: str = "",
        entity_type: str = "",
        entity_id: int | None = None,
        revision: int = 0,
        correlation_id: str = "",
    ) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO kids_audit_events(
                    event, entity_type, entity_id, actor, reason, revision, correlation_id, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event, entity_type, entity_id, actor, reason, revision, correlation_id, utc_now_iso()),
            )
            await db.commit()

    async def kids_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT id, event, entity_type, entity_id, actor, reason, revision, correlation_id, created_at
                FROM kids_audit_events ORDER BY id DESC LIMIT ?
                """,
                (bounded,),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]

    async def kids_watch_event_record(
        self,
        *,
        video_id: str,
        event: str,
        profile: str,
        position_seconds: float | None,
        session_id: str,
        startup_ms: int | None,
        correlation_id: str,
    ) -> dict[str, Any] | None:
        if event not in {"selected", "started", "completed", "stopped"}:
            raise ValueError("invalid Kids watch event")
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            item = await (
                await db.execute(
                    "SELECT id FROM catalog_items WHERE video_id=?",
                    (video_id,),
                )
            ).fetchone()
            if not item:
                return None
            cursor = await db.execute(
                """
                INSERT INTO kids_watch_events(
                    video_id, event, profile, position_seconds, session_id, startup_ms,
                    correlation_id, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id,
                    event,
                    profile,
                    position_seconds,
                    session_id,
                    startup_ms,
                    correlation_id,
                    now,
                ),
            )
            event_id = cursor.lastrowid
            await db.commit()
        return {
            "id": event_id,
            "video_id": video_id,
            "event": event,
            "profile": profile,
            "position_seconds": position_seconds,
            "session_id": session_id,
            "startup_ms": startup_ms,
            "correlation_id": correlation_id,
            "created_at": now,
        }

    async def kids_watch_events_list(self, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT w.*, i.title, i.source_id, s.title AS source_title
                FROM kids_watch_events w
                LEFT JOIN catalog_items i ON i.video_id = w.video_id
                LEFT JOIN catalog_sources s ON s.id = i.source_id
                ORDER BY w.id DESC
                LIMIT ?
                """,
                (bounded,),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]

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
            "policy_flags_json": json.dumps(DEFAULT_POLICY_FLAGS, separators=(",", ":")),
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
            "kids_kill_switch": "false",
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
