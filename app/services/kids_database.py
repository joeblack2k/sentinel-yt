from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from .kids_catalog import (
    KIDS_HOME_SOURCE_REFERENCE,
    _catalog_item_is_authorized,
    _catalog_row_context,
    _parse_utc,
    _quality_height_or_default,
    _stored_candidate_meets_policy,
)
from .time_utils import utc_now_iso


class KidsDatabaseMixin:
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
                    """
                    INSERT INTO catalog_sources(
                        kind,reference,title,language,actor,changed_at,reason,revision,correlation_id
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        values["kind"],
                        values["reference"].strip(),
                        values.get("title", "").strip(),
                        values.get("language", "unknown"),
                        "system",
                        now,
                        "candidate created",
                        revision,
                        values["correlation_id"],
                    ),
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
            if values["state"] in {"blocked", "revoked"}:
                revoke_reason = values["reason"][:1000]
                if entity == "source":
                    await db.execute(
                        """
                        UPDATE relay_leases
                        SET state='revoked',revoked_reason=?,heartbeat_at=?
                        WHERE state='active' AND item_id IN (
                            SELECT id FROM catalog_items WHERE source_id=?
                        )
                        """,
                        (revoke_reason, now, entity_id),
                    )
                else:
                    await db.execute(
                        """
                        UPDATE relay_leases
                        SET state='revoked',revoked_reason=?,heartbeat_at=?
                        WHERE state='active' AND item_id=?
                        """,
                        (revoke_reason, now, entity_id),
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
        language: str = "unknown",
        policy_version: str = "",
        evidence: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        if not isinstance(language, str):
            raise ValueError("invalid source language")
        language = language.strip().lower()
        if language not in {"nl", "en", "mixed", "unknown"}:
            raise ValueError("invalid source language")
        verdict = verdict.strip().upper()
        if verdict not in {"SAFE", "UNSAFE", "UNCERTAIN"}:
            raise ValueError("invalid source safety verdict")
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute("SELECT id FROM catalog_sources WHERE id=?", (source_id,))
                ).fetchone()
                if not row:
                    await db.rollback()
                    return None
                revision = await self._catalog_revision(db)
                await db.execute(
                    """
                    UPDATE catalog_sources
                    SET language=?, safety_verdict=?, safety_reason=?, safety_checked_at=?,
                        safety_policy_version=?, safety_evidence_json=?, safety_sample_count=?,
                        actor=?, changed_at=?, reason=?, revision=?, correlation_id=?
                    WHERE id=?
                    """,
                    (
                        language,
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
                if verdict != "SAFE":
                    await db.execute(
                        """
                        UPDATE relay_leases
                        SET state='revoked',revoked_reason=?,heartbeat_at=?
                        WHERE state='active' AND item_id IN (
                            SELECT id FROM catalog_items WHERE source_id=?
                        )
                        """,
                        (reason[:1000], now, source_id),
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
            except Exception:
                await db.rollback()
                raise
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

    async def kids_feed_session_create(
        self,
        *,
        profile: str,
        policy_version: str,
        minimum_remaining_seconds: int,
        minimum_quality_height: int = 720,
        expires_in_seconds: int = 4 * 60 * 60,
        include_items: bool = True,
    ) -> dict[str, Any]:
        minimum_quality_height = _quality_height_or_default(minimum_quality_height)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        expires_at = (now + timedelta(seconds=max(1, int(expires_in_seconds)))).isoformat()
        expires_after = (
            now + timedelta(seconds=max(0, int(minimum_remaining_seconds)))
        ).isoformat()
        session_id = secrets.token_urlsafe(24)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            revision_row = await (
                await db.execute("SELECT value FROM catalog_meta WHERE key='revision'")
            ).fetchone()
            revision = int(revision_row[0]) if revision_row else 0
            await db.execute(
                """
                INSERT INTO feed_sessions(
                    id,profile,catalog_revision,policy_version,created_at,expires_at
                ) VALUES(?,?,?,?,?,?)
                """,
                (session_id, profile, revision, policy_version, now_iso, expires_at),
            )
            if include_items:
                cur = await db.execute(
                    """
                    SELECT i.*,s.kind AS _source_kind,s.reference AS _source_reference,
                           s.title AS _source_title,s.state AS _source_state,
                           s.safety_verdict AS _source_safety_verdict,
                           b.candidate_json AS _candidate_json,
                           b.quality_height AS _backlog_quality_height
                    FROM catalog_items i JOIN catalog_sources s ON s.id=i.source_id
                    JOIN kids_resolve_backlog b ON b.item_id=i.id
                    WHERE i.state='approved' AND s.state='approved'
                      AND s.safety_verdict='SAFE' AND s.reference!=?
                      AND b.status='ready' AND b.quality_height>=? AND b.expires_at>?
                    ORDER BY b.resolved_at DESC,i.id ASC
                    """,
                    (KIDS_HOME_SOURCE_REFERENCE, minimum_quality_height, expires_after),
                )
                rows = await cur.fetchall()
                columns = [description[0] for description in cur.description]
                ordinal = 0
                for row in rows:
                    item, source, candidate_json = _catalog_row_context(row, columns)
                    quality_height = item.pop("_backlog_quality_height", None)
                    if not (
                        _catalog_item_is_authorized(item, source)
                        and _stored_candidate_meets_policy(
                            candidate_json,
                            quality_height,
                            minimum_quality_height,
                        )
                    ):
                        continue
                    await db.execute(
                        """
                        INSERT INTO feed_session_items(
                            feed_session_id,ordinal,item_id,asset_id
                        ) VALUES(?,?,?,?)
                        """,
                        (session_id, ordinal, item["id"], secrets.token_urlsafe(24)),
                    )
                    ordinal += 1
            await db.commit()
        return {
            "id": session_id,
            "profile": profile,
            "catalog_revision": revision,
            "policy_version": policy_version,
            "created_at": now_iso,
            "expires_at": expires_at,
        }

    async def kids_feed_session_page(
        self,
        session_id: str,
        *,
        profile: str,
        offset: int,
        limit: int,
        policy_version: str,
        minimum_remaining_seconds: int,
        minimum_quality_height: int = 720,
    ) -> dict[str, Any]:
        minimum_quality_height = _quality_height_or_default(minimum_quality_height)
        bounded_offset = max(0, int(offset))
        bounded_limit = max(1, min(int(limit), 60))
        now = datetime.now(timezone.utc)
        expires_after = (
            now + timedelta(seconds=max(0, int(minimum_remaining_seconds)))
        ).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            session = await (
                await db.execute(
                    """
                    SELECT profile,catalog_revision,policy_version,expires_at
                    FROM feed_sessions WHERE id=?
                    """,
                    (session_id,),
                )
            ).fetchone()
            if not session:
                return {"status": "not_found"}
            session_profile, session_revision, session_policy, session_expires = session
            parsed_expiry = _parse_utc(session_expires)
            if parsed_expiry is None or parsed_expiry <= now:
                return {"status": "expired"}
            if session_profile != profile:
                return {"status": "profile_mismatch"}
            if session_policy != policy_version:
                return {"status": "policy_mismatch"}
            revision_row = await (
                await db.execute("SELECT value FROM catalog_meta WHERE key='revision'")
            ).fetchone()
            revision = int(revision_row[0]) if revision_row else 0
            if int(session_revision) != revision:
                return {"status": "stale_revision"}
            # Re-check authorization after session creation. A later
            # blocklist/revoke can invalidate early ordinals, so scan forward
            # in batches until the page is full or the session is exhausted.
            page: list[dict[str, Any]] = []
            scan_ordinal = bounded_offset
            batch_size = max(64, bounded_limit + 1)
            while len(page) < bounded_limit + 1:
                cur = await db.execute(
                    """
                    SELECT f.asset_id,f.ordinal,
                           i.id AS item_id,i.video_id,i.title,i.channel_id,i.channel_title,
                           i.thumbnail_url,i.duration_seconds,i.visual_category,i.state,
                           s.kind AS _source_kind,s.reference AS _source_reference,
                           s.title AS _source_title,s.state AS _source_state,
                           s.safety_verdict AS _source_safety_verdict,
                           b.candidate_json AS _candidate_json,
                           b.quality_height AS _backlog_quality_height
                    FROM feed_session_items f
                    JOIN catalog_items i ON i.id=f.item_id
                    JOIN catalog_sources s ON s.id=i.source_id
                    JOIN kids_resolve_backlog b ON b.item_id=i.id
                    WHERE f.feed_session_id=? AND f.ordinal>=?
                      AND i.state='approved' AND s.state='approved'
                      AND s.safety_verdict='SAFE' AND s.reference!=?
                      AND b.status='ready' AND b.expires_at>?
                    ORDER BY f.ordinal ASC
                    LIMIT ?
                    """,
                    (
                        session_id,
                        scan_ordinal,
                        KIDS_HOME_SOURCE_REFERENCE,
                        expires_after,
                        batch_size,
                    ),
                )
                rows = await cur.fetchall()
                columns = [description[0] for description in cur.description]
                if not rows:
                    break
                for row in rows:
                    values = dict(zip(columns, row))
                    item = {
                        "id": values["item_id"],
                        "state": values["state"],
                        "video_id": values["video_id"],
                        "title": values["title"],
                        "channel_id": values["channel_id"],
                        "channel_title": values["channel_title"],
                    }
                    source = {
                        "kind": values["_source_kind"],
                        "reference": values["_source_reference"],
                        "title": values["_source_title"],
                        "state": values["_source_state"],
                        "safety_verdict": values["_source_safety_verdict"],
                    }
                    if not (
                        _catalog_item_is_authorized(item, source)
                        and _stored_candidate_meets_policy(
                            values["_candidate_json"],
                            values["_backlog_quality_height"],
                            minimum_quality_height,
                        )
                    ):
                        continue
                    page.append(
                        {
                            "asset_id": values["asset_id"],
                            "ordinal": int(values["ordinal"]),
                            "thumbnail_url": values["thumbnail_url"] or "",
                            "duration_seconds": max(
                                0, int(values["duration_seconds"] or 0)
                            ),
                            "visual_category": str(
                                values["visual_category"] or "general"
                            ),
                        }
                    )
                    if len(page) >= bounded_limit + 1:
                        break
                scan_ordinal = int(rows[-1][1]) + 1
                if len(rows) < batch_size:
                    break
        next_offset = None
        if len(page) > bounded_limit:
            page = page[:bounded_limit]
            next_offset = page[-1]["ordinal"] + 1
        for item in page:
            item.pop("ordinal", None)
        return {"status": "ok", "items": page, "next_offset": next_offset}

    async def kids_feed_asset(
        self,
        asset_id: str,
        *,
        require_current_authorization: bool = True,
        minimum_remaining_seconds: int = 0,
        minimum_quality_height: int = 720,
    ) -> dict[str, Any] | None:
        minimum_quality_height = _quality_height_or_default(minimum_quality_height)
        now = datetime.now(timezone.utc)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT f.asset_id,f.feed_session_id,fs.profile,fs.catalog_revision,
                       fs.policy_version,fs.expires_at,
                       i.id AS item_id,i.video_id,i.title,i.channel_id,i.channel_title,
                       i.thumbnail_url,i.duration_seconds,i.visual_category,i.state,
                       s.kind AS _source_kind,s.reference AS _source_reference,
                       s.title AS _source_title,s.state AS _source_state,
                       s.safety_verdict AS _source_safety_verdict,
                       b.candidate_json AS _candidate_json,
                       b.quality_height AS _backlog_quality_height,
                       b.status AS _backlog_status,b.expires_at AS _backlog_expires_at,
                       b.codec
                FROM feed_session_items f
                JOIN feed_sessions fs ON fs.id=f.feed_session_id
                JOIN catalog_items i ON i.id=f.item_id
                JOIN catalog_sources s ON s.id=i.source_id
                LEFT JOIN kids_resolve_backlog b ON b.item_id=i.id
                WHERE f.asset_id=? AND fs.expires_at>?
                """,
                (
                    asset_id,
                    (now + timedelta(seconds=max(0, int(minimum_remaining_seconds)))).isoformat(),
                ),
            )
            row = await cur.fetchone()
            columns = [description[0] for description in cur.description] if row else []
        if not row:
            return None
        values = dict(zip(columns, row))
        item = {
            "id": values["item_id"],
            "state": values["state"],
            "video_id": values["video_id"],
            "title": values["title"],
            "channel_id": values["channel_id"],
            "channel_title": values["channel_title"],
        }
        source = {
            "kind": values["_source_kind"],
            "reference": values["_source_reference"],
            "title": values["_source_title"],
            "state": values["_source_state"],
            "safety_verdict": values["_source_safety_verdict"],
        }
        if require_current_authorization and (
            not _catalog_item_is_authorized(item, source)
            or values["_backlog_status"] != "ready"
            or _parse_utc(values["_backlog_expires_at"]) is None
            or _parse_utc(values["_backlog_expires_at"])
            <= now + timedelta(seconds=max(0, int(minimum_remaining_seconds)))
            or not _stored_candidate_meets_policy(
                values["_candidate_json"],
                values["_backlog_quality_height"],
                minimum_quality_height,
            )
        ):
            return None
        return {
            "asset_id": values["asset_id"],
            "feed_session_id": values["feed_session_id"],
            "profile": values["profile"],
            "catalog_revision": int(values["catalog_revision"]),
            "policy_version": values["policy_version"],
            "item_id": int(values["item_id"]),
            "video_id": values["video_id"],
            "thumbnail_url": values["thumbnail_url"] or "",
            "duration_seconds": max(0, int(values["duration_seconds"] or 0)),
            "visual_category": str(values["visual_category"] or "general"),
            "candidate_json": values["_candidate_json"],
            "quality_height": values["_backlog_quality_height"],
            "codec": values["codec"] or "",
        }

    async def kids_relay_lease_create(
        self,
        *,
        asset_id: str,
        policy_version: str,
        minimum_remaining_seconds: int,
        minimum_quality_height: int = 720,
        expires_in_seconds: int = 2 * 60 * 60,
    ) -> dict[str, Any]:
        minimum_quality_height = _quality_height_or_default(minimum_quality_height)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        candidate_after = (
            now + timedelta(seconds=max(0, int(minimum_remaining_seconds)))
        ).isoformat()
        lease_deadline = now + timedelta(seconds=max(1, int(expires_in_seconds)))
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                """
                SELECT f.feed_session_id,fs.profile,fs.catalog_revision,
                       fs.policy_version,fs.expires_at,
                       i.id AS item_id,i.video_id,i.title,i.channel_id,i.channel_title,i.state,
                       s.kind AS _source_kind,s.reference AS _source_reference,
                       s.title AS _source_title,s.state AS _source_state,
                       s.safety_verdict AS _source_safety_verdict,
                       b.status AS _backlog_status,b.candidate_json,
                       b.quality_height,b.codec,b.expires_at AS candidate_expires_at
                FROM feed_session_items f
                JOIN feed_sessions fs ON fs.id=f.feed_session_id
                JOIN catalog_items i ON i.id=f.item_id
                JOIN catalog_sources s ON s.id=i.source_id
                LEFT JOIN kids_resolve_backlog b ON b.item_id=i.id
                WHERE f.asset_id=?
                """,
                (asset_id,),
            )
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return {"status": "not_found"}
            values = dict(zip([description[0] for description in cur.description], row))
            session_expiry = _parse_utc(values["expires_at"])
            if session_expiry is None or session_expiry <= now:
                await db.rollback()
                return {"status": "expired"}
            if values["policy_version"] != policy_version:
                await db.rollback()
                return {"status": "policy_mismatch"}
            revision_row = await (
                await db.execute("SELECT value FROM catalog_meta WHERE key='revision'")
            ).fetchone()
            revision = int(revision_row[0]) if revision_row else 0
            if int(values["catalog_revision"]) != revision:
                await db.rollback()
                return {"status": "stale_revision"}
            kill_switch = await (
                await db.execute(
                    "SELECT value FROM settings WHERE key='kids_kill_switch'"
                )
            ).fetchone()
            if kill_switch and str(kill_switch[0]).strip().lower() not in {
                "",
                "0",
                "false",
                "off",
                "no",
            }:
                await db.rollback()
                return {"status": "kill_switch"}
            item = {
                "id": values["item_id"],
                "state": values["state"],
                "video_id": values["video_id"],
                "title": values["title"],
                "channel_id": values["channel_id"],
                "channel_title": values["channel_title"],
            }
            source = {
                "kind": values["_source_kind"],
                "reference": values["_source_reference"],
                "title": values["_source_title"],
                "state": values["_source_state"],
                "safety_verdict": values["_source_safety_verdict"],
            }
            if not _catalog_item_is_authorized(item, source):
                await db.rollback()
                return {"status": "ineligible"}
            if (
                values["_backlog_status"] != "ready"
                or not _stored_candidate_meets_policy(
                    values["candidate_json"],
                    values["quality_height"],
                    minimum_quality_height,
                )
                or _parse_utc(values["candidate_expires_at"]) is None
                or _parse_utc(values["candidate_expires_at"]) <= datetime.fromisoformat(candidate_after)
            ):
                await db.rollback()
                return {"status": "candidate_unavailable"}
            try:
                candidate = json.loads(str(values["candidate_json"]))
            except (TypeError, json.JSONDecodeError):
                await db.rollback()
                return {"status": "candidate_unavailable"}
            candidate_expiry = _parse_utc(values["candidate_expires_at"])
            if candidate_expiry is None:
                await db.rollback()
                return {"status": "candidate_unavailable"}
            lease_expires_at = min(lease_deadline, candidate_expiry).isoformat()
            lease_id = secrets.token_urlsafe(24)
            await db.execute(
                """
                INSERT INTO relay_leases(
                    id,item_id,feed_session_id,state,candidate_json,quality_height,
                    revoked_reason,created_at,expires_at,heartbeat_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    lease_id,
                    values["item_id"],
                    values["feed_session_id"],
                    "active",
                    json.dumps(candidate, separators=(",", ":"), ensure_ascii=True),
                    values["quality_height"],
                    "",
                    now_iso,
                    lease_expires_at,
                    now_iso,
                ),
            )
            await db.commit()
        return {
            "status": "ok",
            "id": lease_id,
            "session_id": lease_id,
            "quality_height": int(values["quality_height"]),
            "codec": str(values["codec"] or ""),
            "expires_at": lease_expires_at,
        }

    async def kids_relay_lease_get(
        self,
        lease_id: str,
        *,
        minimum_quality_height: int = 720,
    ) -> dict[str, Any] | None:
        minimum_quality_height = _quality_height_or_default(minimum_quality_height)
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                """
                SELECT l.id,l.item_id,l.feed_session_id,l.state,l.candidate_json,
                       l.quality_height,l.expires_at,
                       i.video_id,i.title,i.channel_id,i.channel_title,i.state AS item_state,
                       s.kind AS _source_kind,s.reference AS _source_reference,
                       s.title AS _source_title,s.state AS _source_state,
                       s.safety_verdict AS _source_safety_verdict
                FROM relay_leases l
                JOIN catalog_items i ON i.id=l.item_id
                JOIN catalog_sources s ON s.id=i.source_id
                WHERE l.id=?
                """,
                (lease_id,),
            )
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return None
            values = dict(zip([description[0] for description in cur.description], row))
            if values["state"] != "active":
                await db.rollback()
                return None
            expires_at = _parse_utc(values["expires_at"])
            if expires_at is None or expires_at <= now:
                await db.execute(
                    """
                    UPDATE relay_leases
                    SET state='expired',revoked_reason='lease_expired',heartbeat_at=?
                    WHERE id=? AND state='active'
                    """,
                    (now_iso, lease_id),
                )
                await db.commit()
                return None
            kill_switch = await (
                await db.execute(
                    "SELECT value FROM settings WHERE key='kids_kill_switch'"
                )
            ).fetchone()
            if kill_switch and str(kill_switch[0]).strip().lower() not in {
                "",
                "0",
                "false",
                "off",
                "no",
            }:
                await db.execute(
                    """
                    UPDATE relay_leases
                    SET state='revoked',revoked_reason='kill_switch',heartbeat_at=?
                    WHERE id=? AND state='active'
                    """,
                    (now_iso, lease_id),
                )
                await db.commit()
                return None
            item = {
                "state": values["item_state"],
                "video_id": values["video_id"],
                "title": values["title"],
                "channel_id": values["channel_id"],
                "channel_title": values["channel_title"],
            }
            source = {
                "kind": values["_source_kind"],
                "reference": values["_source_reference"],
                "title": values["_source_title"],
                "state": values["_source_state"],
                "safety_verdict": values["_source_safety_verdict"],
            }
            try:
                candidate = json.loads(str(values["candidate_json"]))
            except (TypeError, json.JSONDecodeError):
                candidate = None
            if not (
                _catalog_item_is_authorized(item, source)
                and _stored_candidate_meets_policy(
                    values["candidate_json"],
                    values["quality_height"],
                    minimum_quality_height,
                )
                and isinstance(candidate, dict)
            ):
                await db.execute(
                    """
                    UPDATE relay_leases
                    SET state='revoked',revoked_reason='catalog_ineligible',heartbeat_at=?
                    WHERE id=? AND state='active'
                    """,
                    (now_iso, lease_id),
                )
                await db.commit()
                return None
            await db.execute(
                "UPDATE relay_leases SET heartbeat_at=? WHERE id=? AND state='active'",
                (now_iso, lease_id),
            )
            await db.commit()
        return {
            "id": values["id"],
            "item_id": int(values["item_id"]),
            "feed_session_id": values["feed_session_id"],
            "video_id": values["video_id"],
            "candidate": candidate,
            "quality_height": int(values["quality_height"]),
            "expires_at": values["expires_at"],
        }

    async def kids_relay_lease_close(self, lease_id: str) -> bool:
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                """
                UPDATE relay_leases
                SET state='closed',revoked_reason='client_closed',heartbeat_at=?
                WHERE id=? AND state='active'
                """,
                (now, lease_id),
            )
            await db.commit()
            return cur.rowcount > 0

    async def kids_relay_lease_item_id(self, lease_id: str) -> int | None:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute("SELECT item_id FROM relay_leases WHERE id=?", (lease_id,))
            ).fetchone()
        return int(row[0]) if row else None

    async def kids_relay_lease_event_context(
        self,
        lease_id: str,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT l.item_id,l.feed_session_id,l.state,fs.profile
                    FROM relay_leases l
                    JOIN feed_sessions fs ON fs.id=l.feed_session_id
                    WHERE l.id=?
                    """,
                    (lease_id,),
                )
            ).fetchone()
        if not row:
            return None
        return {
            "item_id": int(row[0]),
            "feed_session_id": str(row[1]),
            "state": str(row[2]),
            "profile": str(row[3]),
        }

    async def kids_revoke_active_leases(self, *, reason: str) -> int:
        now = utc_now_iso()
        safe_reason = str(reason or "policy_closed")[:1000]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute(
                """
                UPDATE relay_leases
                SET state='revoked',revoked_reason=?,heartbeat_at=?
                WHERE state='active'
                """,
                (safe_reason, now),
            )
            revoked = int(cur.rowcount)
            if revoked:
                await db.execute(
                    """
                    INSERT INTO kids_audit_events(
                        event,entity_type,entity_id,actor,reason,revision,correlation_id,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        "relay_leases_revoked",
                        "policy",
                        None,
                        "system",
                        f"{safe_reason} ({revoked})",
                        0,
                        "",
                        now,
                    ),
                )
            await db.commit()
        return revoked

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
                    await db.execute(
                        """
                        UPDATE relay_leases
                        SET state='revoked',
                            revoked_reason=CASE
                                WHEN trim(coalesce(revoked_reason,''))='' THEN 'catalog_ineligible'
                                ELSE revoked_reason
                            END,
                            heartbeat_at=?
                        WHERE item_id=? AND state='active'
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
                UPDATE relay_leases
                SET state='revoked',
                    revoked_reason=CASE
                        WHEN trim(coalesce(revoked_reason,''))='' THEN 'catalog_ineligible'
                        ELSE revoked_reason
                    END,
                    heartbeat_at=?
                WHERE state='active' AND item_id IN (
                    SELECT i.id
                    FROM catalog_items i
                    LEFT JOIN catalog_sources s ON s.id=i.source_id
                    WHERE i.state != 'approved'
                       OR s.id IS NULL
                       OR s.state != 'approved'
                       OR s.safety_verdict != 'SAFE'
                       OR s.reference=?
                )
                """,
                (now, KIDS_HOME_SOURCE_REFERENCE),
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
        minimum_quality_height: int = 720,
    ) -> None:
        if (
            type(minimum_quality_height) is not int
            or not 720 <= minimum_quality_height <= 1080
            or type(quality_height) is not int
            or not minimum_quality_height <= quality_height <= 1080
            or not isinstance(candidate, dict)
            or candidate.get("quality_height") != quality_height
        ):
            return
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
        return (value or "true").strip().lower() not in {"", "0", "false", "off", "no"}

    async def set_kids_kill_switch(
        self,
        *,
        enabled: bool,
        actor: str,
        reason: str,
        correlation_id: str,
    ) -> bool:
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                "INSERT INTO settings(key, value) VALUES('kids_kill_switch', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("true" if enabled else "false",),
            )
            revoked = 0
            if enabled:
                cur = await db.execute(
                    """
                    UPDATE relay_leases
                    SET state='revoked',revoked_reason='kill_switch',heartbeat_at=?
                    WHERE state='active'
                    """,
                    (now,),
                )
                revoked = int(cur.rowcount)
            await db.execute(
                """
                INSERT INTO kids_audit_events(
                    event, entity_type, entity_id, actor, reason, revision, correlation_id, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("kill_switch_changed", "control", None, actor, reason, 0, correlation_id, now),
            )
            if revoked:
                await db.execute(
                    """
                    INSERT INTO kids_audit_events(
                        event,entity_type,entity_id,actor,reason,revision,correlation_id,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        "relay_leases_revoked",
                        "policy",
                        None,
                        actor,
                        f"kill_switch ({revoked})",
                        0,
                        correlation_id,
                        now,
                    ),
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
