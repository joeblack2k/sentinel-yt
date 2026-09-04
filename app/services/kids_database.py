from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import aiosqlite

from .kids_catalog import (
    KIDS_HOME_SOURCE_REFERENCE,
    _catalog_item_is_authorized,
    _catalog_row_context,
    _parse_utc,
    _quality_height_or_default,
    _stored_candidate_meets_policy,
    kids_source_url,
    kids_video_url,
)
from .time_utils import utc_now_iso


DEFAULT_KIDS_PROFILES = (
    ("noah", "Noah", 6, "hare.fill", 0),
    ("felix", "Felix", 2, "tortoise.fill", 1),
)
DEFAULT_KIDS_PROFILE_SLUGS = frozenset(profile[0] for profile in DEFAULT_KIDS_PROFILES)
KIDS_CHANNEL_ART_HOSTS = frozenset(
    {
        "yt3.ggpht.com",
        "yt4.ggpht.com",
        "yt3.googleusercontent.com",
        "yt4.googleusercontent.com",
    }
)
KIDS_VIDEO_THUMBNAIL_HOSTS = frozenset({"i.ytimg.com"})


def _profile_slugs(value: Any) -> list[str]:
    return [part for part in str(value or "").split(",") if part]


def _youtube_source_url(kind: Any, reference: Any) -> str:
    raw = str(reference or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        if parsed.hostname != "www.youtubekids.com":
            return ""
        parts = [part for part in parsed.path.split("/") if part]
        if kind == "channel" and len(parts) == 2 and parts[0] == "channel":
            return f"https://www.youtube.com/channel/{quote(parts[1], safe='')}"
        if kind == "playlist":
            playlist_id = parse_qs(parsed.query).get("list", [""])[0]
            if playlist_id:
                return f"https://www.youtube.com/playlist?list={quote(playlist_id, safe='')}"
        return ""
    if kind == "channel" and raw.startswith("UC"):
        return f"https://www.youtube.com/channel/{quote(raw, safe='')}"
    if kind == "playlist" and raw.startswith("PL"):
        return f"https://www.youtube.com/playlist?list={quote(raw, safe='')}"
    return ""


def _kids_thumbnail_is_proxyable(value: Any) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in KIDS_VIDEO_THUMBNAIL_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port is None
    )


def _kids_channel_avatar_is_proxyable(value: Any) -> bool:
    try:
        parsed = urlparse(str(value or "").strip())
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in KIDS_CHANNEL_ART_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port is None
    )


def _kids_source_can_publish_poster(source: dict[str, Any]) -> bool:
    return (
        source.get("kind") == "channel"
        and source.get("state") == "approved"
        and source.get("safety_verdict") == "SAFE"
        and source.get("reference") != KIDS_HOME_SOURCE_REFERENCE
    )


def _kids_effective_poster_item(
    poster_item_id: Any,
    items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if poster_item_id is not None:
        for item in items:
            if item["id"] == poster_item_id:
                return item
    return items[0] if items else None


class KidsDatabaseMixin:
    async def _ensure_kids_profile_defaults(self) -> None:
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.executemany(
                """
                INSERT OR IGNORE INTO kids_profiles(
                    slug,display_name,age_years,avatar_key,enabled,sort_order,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                [
                    (slug, name, age, avatar, 1, order, now, now)
                    for slug, name, age, avatar, order in DEFAULT_KIDS_PROFILES
                ],
            )
            await db.executemany(
                """
                UPDATE kids_profiles
                SET avatar_key=?,updated_at=?
                WHERE slug=? AND avatar_key IN ('',?)
                """,
                [
                    (avatar, now, slug, slug)
                    for slug, _name, _age, avatar, _order in DEFAULT_KIDS_PROFILES
                ],
            )
            marker = await (
                await db.execute(
                    "SELECT value FROM settings WHERE key='kids_profile_backfill_v1'"
                )
            ).fetchone()
            if marker is None:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO catalog_source_profiles(
                        source_id,profile_slug,actor,reason,assigned_at
                    )
                    SELECT id,'noah','kids-profile-migration',
                           'Existing source retained for Noah during profile rollout',?
                    FROM catalog_sources
                    WHERE reference != ?
                    """,
                    (now, KIDS_HOME_SOURCE_REFERENCE),
                )
                await db.execute(
                    """
                    INSERT INTO settings(key,value) VALUES('kids_profile_backfill_v1','1')
                    ON CONFLICT(key) DO NOTHING
                    """
                )
            await db.commit()

    async def kids_profile_get(self, profile: str) -> dict[str, Any] | None:
        slug = str(profile or "").strip().lower()
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    """
                    SELECT slug,display_name,age_years,avatar_key,enabled,sort_order,
                           created_at,updated_at
                    FROM kids_profiles WHERE slug=?
                    """,
                    (slug,),
                )
            ).fetchone()
            if row is None:
                return None
            source_count = await (
                await db.execute(
                    """
                    SELECT COUNT(*)
                    FROM catalog_source_profiles ps
                    JOIN catalog_sources s ON s.id=ps.source_id
                    WHERE ps.profile_slug=? AND s.reference != ?
                    """,
                    (slug, KIDS_HOME_SOURCE_REFERENCE),
                )
            ).fetchone()
            approved_count = await (
                await db.execute(
                    """
                    SELECT COUNT(*)
                    FROM catalog_source_profiles ps
                    JOIN catalog_sources s ON s.id=ps.source_id
                    WHERE ps.profile_slug=? AND ps.source_id != 0
                      AND s.state='approved' AND s.safety_verdict='SAFE'
                      AND s.reference != ?
                    """,
                    (slug, KIDS_HOME_SOURCE_REFERENCE),
                )
            ).fetchone()
        return {
            "slug": row[0],
            "display_name": row[1],
            "age_years": int(row[2]),
            "avatar_key": row[3] or "",
            "enabled": bool(row[4]),
            "sort_order": int(row[5]),
            "created_at": row[6],
            "updated_at": row[7],
            "source_count": int(source_count[0] if source_count else 0),
            "approved_source_count": int(approved_count[0] if approved_count else 0),
        }

    async def kids_profiles_list(self) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT p.slug,p.display_name,p.age_years,p.avatar_key,p.enabled,
                       p.sort_order,p.created_at,p.updated_at,
                       COUNT(DISTINCT CASE
                           WHEN s.reference != ? THEN ps.source_id END) AS source_count,
                       COUNT(DISTINCT CASE
                           WHEN s.state='approved' AND s.safety_verdict='SAFE'
                                AND s.reference != ?
                           THEN s.id END) AS approved_source_count
                FROM kids_profiles p
                LEFT JOIN catalog_source_profiles ps ON ps.profile_slug=p.slug
                LEFT JOIN catalog_sources s ON s.id=ps.source_id
                GROUP BY p.slug
                ORDER BY p.sort_order ASC,p.slug ASC
                """,
                (KIDS_HOME_SOURCE_REFERENCE, KIDS_HOME_SOURCE_REFERENCE),
            )
            rows = await cur.fetchall()
            columns = [description[0] for description in cur.description]
        return [
            {
                **dict(zip(columns, row)),
                "age_years": int(row[2]),
                "enabled": bool(row[4]),
                "sort_order": int(row[5]),
                "source_count": int(row[8]),
                "approved_source_count": int(row[9]),
            }
            for row in rows
        ]

    async def kids_source_profile_slugs(self, source_id: int) -> list[str]:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT profile_slug
                FROM catalog_source_profiles
                WHERE source_id=?
                ORDER BY profile_slug ASC
                """,
                (source_id,),
            )
            return [str(row[0]) for row in await cur.fetchall()]

    async def kids_source_profiles_set(
        self,
        source_id: int,
        profiles: list[str],
        *,
        actor: str,
        reason: str,
        correlation_id: str,
    ) -> dict[str, Any] | None:
        requested = list(dict.fromkeys(str(profile or "").strip().lower() for profile in profiles))
        if any(profile not in DEFAULT_KIDS_PROFILE_SLUGS for profile in requested):
            raise ValueError("unknown Kids profile")
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            source = await (
                await db.execute(
                    "SELECT id,kind,reference FROM catalog_sources WHERE id=?",
                    (source_id,),
                )
            ).fetchone()
            if source is None:
                await db.rollback()
                return None
            if source[2] == KIDS_HOME_SOURCE_REFERENCE:
                await db.rollback()
                raise ValueError("the YouTube Kids home source cannot be assigned to a profile")
            cur = await db.execute(
                """
                SELECT profile_slug
                FROM catalog_source_profiles
                WHERE source_id=?
                """,
                (source_id,),
            )
            current = {str(row[0]) for row in await cur.fetchall()}
            desired = set(requested)
            if current != desired:
                revision = await self._catalog_revision(db)
                if desired:
                    placeholders = ",".join("?" for _ in desired)
                    await db.execute(
                        f"""
                        DELETE FROM catalog_source_profiles
                        WHERE source_id=? AND profile_slug NOT IN ({placeholders})
                        """,
                        (source_id, *sorted(desired)),
                    )
                else:
                    await db.execute(
                        "DELETE FROM catalog_source_profiles WHERE source_id=?",
                        (source_id,),
                    )
                for profile in sorted(desired):
                    await db.execute(
                        """
                        INSERT INTO catalog_source_profiles(
                            source_id,profile_slug,actor,reason,assigned_at
                        ) VALUES(?,?,?,?,?)
                        ON CONFLICT(source_id,profile_slug) DO UPDATE SET
                            actor=excluded.actor,
                            reason=excluded.reason,
                            assigned_at=excluded.assigned_at
                        """,
                        (source_id, profile, actor, reason[:1000], now),
                    )
                removed = current - desired
                for profile in sorted(removed):
                    await db.execute(
                        """
                        UPDATE relay_leases
                        SET state='revoked',revoked_reason='profile_unassigned',heartbeat_at=?
                        WHERE state='active'
                          AND item_id IN (
                              SELECT id FROM catalog_items WHERE source_id=?
                          )
                          AND feed_session_id IN (
                              SELECT id FROM feed_sessions WHERE profile=?
                          )
                        """,
                        (now, source_id, profile),
                    )
                await db.execute(
                    """
                    INSERT INTO kids_audit_events(
                        event,entity_type,entity_id,actor,reason,revision,correlation_id,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        "source_profiles_changed",
                        "source",
                        source_id,
                        actor,
                        f"{reason[:900]} profiles={','.join(sorted(desired)) or 'none'}",
                        revision,
                        correlation_id,
                        now,
                    ),
                )
            await db.commit()
        source_row = await self.catalog_get("source", source_id)
        if source_row is None:
            return None
        source_row["profile_slugs"] = sorted(desired)
        return source_row

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
        if entity == "source":
            requested_profiles = values.get("profile_slugs", values.get("profiles"))
            if requested_profiles is None:
                requested_profiles = ["noah"]
            if not isinstance(requested_profiles, list):
                raise ValueError("profiles must be a list")
            requested_profiles = list(
                dict.fromkeys(
                    str(profile or "").strip().lower() for profile in requested_profiles
                )
            )
            if any(profile not in DEFAULT_KIDS_PROFILE_SLUGS for profile in requested_profiles):
                raise ValueError("unknown Kids profile")
            if str(values["reference"]).strip() == KIDS_HOME_SOURCE_REFERENCE:
                requested_profiles = []
        async with aiosqlite.connect(self.db_path) as db:
            revision = await self._catalog_revision(db)
            if entity == "source":
                cur = await db.execute(
                    """
                    INSERT INTO catalog_sources(
                        kind,reference,title,avatar_url,language,
                        actor,changed_at,reason,revision,correlation_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        values["kind"],
                        values["reference"].strip(),
                        values.get("title", "").strip(),
                        str(values.get("avatar_url", "") or "").strip()[:2000],
                        values.get("language", "unknown"),
                        "system",
                        now,
                        "candidate created",
                        revision,
                        values["correlation_id"],
                    ),
                )
                entity_id = cur.lastrowid
                for profile in requested_profiles:
                    await db.execute(
                        """
                        INSERT INTO catalog_source_profiles(
                            source_id,profile_slug,actor,reason,assigned_at
                        ) VALUES(?,?,?,?,?)
                        """,
                        (
                            entity_id,
                            profile,
                            "system",
                            "Default profile assignment",
                            now,
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

    async def _kids_source_poster_items_bulk(
        self,
        source_ids: list[int],
    ) -> dict[int, list[dict[str, Any]]]:
        bounded_source_ids = sorted({int(source_id) for source_id in source_ids})
        if not bounded_source_ids:
            return {}
        placeholders = ",".join("?" for _ in bounded_source_ids)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                f"""
                SELECT i.source_id,i.id,i.title,i.thumbnail_url,i.state,
                       i.channel_id,i.channel_title,
                       s.kind,s.reference,s.title,s.state,s.safety_verdict
                FROM catalog_items i
                JOIN catalog_sources s ON s.id=i.source_id
                WHERE i.source_id IN ({placeholders}) AND i.state='approved'
                ORDER BY i.source_id ASC,i.id ASC
                """,
                bounded_source_ids,
            )
            rows = await cur.fetchall()
        result: dict[int, list[dict[str, Any]]] = {}
        for (
            source_id,
            item_id,
            title,
            thumbnail_url,
            state,
            channel_id,
            channel_title,
            source_kind,
            source_reference,
            source_title,
            source_state,
            source_safety_verdict,
        ) in rows:
            thumbnail = str(thumbnail_url or "").strip()
            item = {
                "state": state,
                "channel_id": channel_id,
                "channel_title": channel_title,
            }
            source = {
                "kind": source_kind,
                "reference": source_reference,
                "title": source_title,
                "state": source_state,
                "safety_verdict": source_safety_verdict,
            }
            if (
                not _catalog_item_is_authorized(item, source)
                or not _kids_thumbnail_is_proxyable(thumbnail)
            ):
                continue
            result.setdefault(int(source_id), []).append(
                {
                    "id": int(item_id),
                    "title": str(title or ""),
                    "thumbnail_url": thumbnail,
                    "state": str(state),
                }
            )
        return result

    async def kids_source_poster_items(
        self,
        source_id: int,
    ) -> list[dict[str, Any]] | None:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT id FROM catalog_sources WHERE id=?",
                    (source_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return (await self._kids_source_poster_items_bulk([source_id])).get(
            int(source_id),
            [],
        )

    async def kids_source_poster_state(
        self,
        source_id: int,
    ) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT id,kind,reference,title,avatar_url,poster_item_id,
                       state,safety_verdict,revision
                FROM catalog_sources
                WHERE id=?
                """,
                (source_id,),
            )
            row = await cur.fetchone()
            columns = [description[0] for description in cur.description] if row else []
        if row is None:
            return None
        source = dict(zip(columns, row))
        source["id"] = int(source["id"])
        source["poster_item_id"] = (
            int(source["poster_item_id"])
            if source["poster_item_id"] is not None
            else None
        )
        avatar_url = str(source.get("avatar_url") or "").strip()
        source["genuine_avatar_url"] = (
            avatar_url if _kids_channel_avatar_is_proxyable(avatar_url) else ""
        )
        items = (
            await self._kids_source_poster_items_bulk([source_id])
        ).get(int(source_id), [])
        effective = (
            _kids_effective_poster_item(source["poster_item_id"], items)
            if _kids_source_can_publish_poster(source)
            else None
        )
        source["effective_poster_thumbnail_url"] = (
            effective["thumbnail_url"] if effective else ""
        )
        return {
            "source": source,
            "effective_poster": {
                "mode": (
                    "explicit"
                    if effective is not None
                    and effective["id"] == source["poster_item_id"]
                    else "automatic"
                ),
                "item": effective,
            },
        }

    async def kids_source_poster_set(
        self,
        source_id: int,
        poster_item_id: int | None,
        *,
        actor: str,
        reason: str,
        correlation_id: str,
    ) -> dict[str, Any] | None:
        if poster_item_id is not None and (
            type(poster_item_id) is not int or poster_item_id < 1
        ):
            raise ValueError("invalid poster item")
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                cur = await db.execute(
                    """
                    SELECT id,kind,reference,title,state,safety_verdict,poster_item_id
                    FROM catalog_sources
                    WHERE id=?
                    """,
                    (source_id,),
                )
                source_row = await cur.fetchone()
                if source_row is None:
                    await db.rollback()
                    return None
                source = dict(
                    zip(
                        [
                            "id",
                            "kind",
                            "reference",
                            "title",
                            "state",
                            "safety_verdict",
                            "poster_item_id",
                        ],
                        source_row,
                    )
                )
                if not _kids_source_can_publish_poster(source):
                    raise ValueError("catalog source is not eligible for poster management")
                if poster_item_id is not None:
                    item = await (
                        await db.execute(
                            """
                            SELECT source_id,state,thumbnail_url,channel_id,channel_title
                            FROM catalog_items
                            WHERE id=?
                            """,
                            (poster_item_id,),
                        )
                    ).fetchone()
                    if item is None:
                        raise ValueError("poster item not found")
                    if item[0] != source_id:
                        raise ValueError("poster item belongs to a different source")
                    if item[1] != "approved":
                        raise ValueError("poster item is not approved")
                    if not _kids_thumbnail_is_proxyable(item[2]):
                        raise ValueError("poster item thumbnail is not proxyable")
                    if not _catalog_item_is_authorized(
                        {
                            "state": item[1],
                            "channel_id": item[3],
                            "channel_title": item[4],
                        },
                        source,
                    ):
                        raise ValueError("poster item is not authorized")
                if source["poster_item_id"] == poster_item_id:
                    await db.rollback()
                else:
                    revision = await self._catalog_revision(db)
                    await db.execute(
                        """
                        UPDATE catalog_sources
                        SET poster_item_id=?,actor=?,changed_at=?,reason=?,
                            revision=?,correlation_id=?
                        WHERE id=?
                        """,
                        (
                            poster_item_id,
                            actor,
                            now,
                            reason,
                            revision,
                            correlation_id,
                            source_id,
                        ),
                    )
                    await db.execute(
                        """
                        INSERT INTO kids_audit_events(
                            event,entity_type,entity_id,actor,reason,revision,
                            correlation_id,created_at
                        ) VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (
                            "source_poster_changed",
                            "source",
                            source_id,
                            actor,
                            reason,
                            revision,
                            correlation_id,
                            now,
                        ),
                    )
                    await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.kids_source_poster_state(source_id)

    async def catalog_source_avatar_update(
        self,
        source_id: int,
        avatar_url: str,
        *,
        actor: str,
        reason: str,
        correlation_id: str,
    ) -> dict[str, Any] | None:
        avatar_url = str(avatar_url or "").strip()
        if not _kids_channel_avatar_is_proxyable(avatar_url):
            raise ValueError("channel avatar URL is not trusted")
        if len(avatar_url) > 2000:
            raise ValueError("channel avatar URL is too long")
        now = utc_now_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                row = await (
                    await db.execute(
                        """
                        SELECT kind,reference,state,avatar_url
                        FROM catalog_sources WHERE id=?
                        """,
                        (source_id,),
                    )
                ).fetchone()
                if row is None:
                    await db.rollback()
                    return None
                kind, reference, state, current_avatar_url = row
                if (
                    kind != "channel"
                    or reference == KIDS_HOME_SOURCE_REFERENCE
                    or state not in {"candidate", "approved"}
                ):
                    raise ValueError("catalog source is not eligible for avatar recovery")
                if str(current_avatar_url or "").strip() == avatar_url:
                    await db.rollback()
                else:
                    revision = await self._catalog_revision(db)
                    await db.execute(
                        """
                        UPDATE catalog_sources
                        SET avatar_url=?,actor=?,changed_at=?,reason=?,
                            revision=?,correlation_id=?
                        WHERE id=?
                        """,
                        (
                            avatar_url,
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
                            event,entity_type,entity_id,actor,reason,revision,
                            correlation_id,created_at
                        ) VALUES(?,?,?,?,?,?,?,?)
                        """,
                        (
                            "source_avatar_changed",
                            "source",
                            source_id,
                            actor,
                            reason[:1000],
                            revision,
                            correlation_id,
                            now,
                        ),
                    )
                    await db.commit()
            except Exception:
                await db.rollback()
                raise
        return await self.catalog_get("source", source_id)

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
        profile: str = "noah",
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
                JOIN catalog_source_profiles ps
                  ON ps.source_id=s.id AND ps.profile_slug=?
                JOIN kids_resolve_backlog b ON b.item_id=i.id
                WHERE i.state='approved' AND s.state='approved' AND s.safety_verdict='SAFE'
                  AND s.reference!=? AND b.status='ready' AND b.quality_height>=? AND b.expires_at>?
                ORDER BY
                    ROW_NUMBER() OVER (
                        PARTITION BY i.source_id
                        ORDER BY b.resolved_at DESC,i.id ASC
                    ),
                    MAX(b.resolved_at) OVER (PARTITION BY i.source_id) DESC,
                    b.resolved_at DESC,i.id ASC
                """,
                (
                    profile,
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
        source_id: int | None = None,
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
                    id,profile,source_id,catalog_revision,policy_version,created_at,expires_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (session_id, profile, source_id, revision, policy_version, now_iso, expires_at),
            )
            if include_items:
                source_filter = ""
                source_args: tuple[Any, ...] = ()
                if source_id is not None:
                    source_filter = " AND s.id=?"
                    source_args = (source_id,)
                cur = await db.execute(
                    f"""
                    SELECT i.*,s.kind AS _source_kind,s.reference AS _source_reference,
                           s.title AS _source_title,s.state AS _source_state,
                           s.safety_verdict AS _source_safety_verdict,
                           b.candidate_json AS _candidate_json,
                           b.quality_height AS _backlog_quality_height
                    FROM catalog_items i JOIN catalog_sources s ON s.id=i.source_id
                    JOIN catalog_source_profiles ps
                      ON ps.source_id=s.id AND ps.profile_slug=?
                    JOIN kids_resolve_backlog b ON b.item_id=i.id
                    WHERE i.state='approved' AND s.state='approved'
                      AND s.safety_verdict='SAFE' AND s.reference!=?
                      {source_filter}
                      AND b.status='ready' AND b.quality_height>=? AND b.expires_at>?
                    ORDER BY
                        ROW_NUMBER() OVER (
                            PARTITION BY i.source_id
                            ORDER BY RANDOM()
                        ),
                        RANDOM()
                    """,
                    (
                        profile,
                        KIDS_HOME_SOURCE_REFERENCE,
                        *source_args,
                        minimum_quality_height,
                        expires_after,
                    ),
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

    async def kids_feed_session_binding(self, session_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            row = await (
                await db.execute(
                    "SELECT profile,source_id FROM feed_sessions WHERE id=?",
                    (session_id,),
                )
            ).fetchone()
        if row is None:
            return None
        return {
            "profile": str(row[0]),
            "source_id": int(row[1]) if row[1] is not None else None,
        }

    async def kids_eligible_channels(
        self,
        *,
        profile: str,
        minimum_remaining_seconds: int,
        minimum_quality_height: int = 720,
    ) -> list[dict[str, Any]]:
        """Return current profile-bound sources with a usable approved poster."""
        minimum_quality_height = _quality_height_or_default(minimum_quality_height)
        now = datetime.now(timezone.utc)
        expires_after = now + timedelta(seconds=max(0, int(minimum_remaining_seconds)))
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """
                SELECT s.id,s.kind,s.reference,s.title,s.avatar_url,s.poster_item_id,
                       s.state,s.safety_verdict,
                       i.id AS _item_id,i.video_id,i.title AS _item_title,
                       i.channel_id,i.channel_title,i.state AS _item_state,
                       i.thumbnail_url,
                       b.status AS _backlog_status,b.candidate_json AS _candidate_json,
                       b.quality_height AS _backlog_quality_height,
                       b.expires_at AS _backlog_expires_at
                FROM catalog_sources s
                JOIN catalog_source_profiles ps
                  ON ps.source_id=s.id AND ps.profile_slug=?
                JOIN catalog_items i ON i.source_id=s.id
                JOIN kids_resolve_backlog b ON b.item_id=i.id
                WHERE s.state='approved' AND s.safety_verdict='SAFE'
                  AND s.kind='channel' AND s.reference!=? AND b.status='ready'
                  AND b.expires_at>?
                ORDER BY s.id ASC,i.id ASC
                """,
                (
                    profile,
                    KIDS_HOME_SOURCE_REFERENCE,
                    expires_after.isoformat(),
                ),
            )
            rows = await cur.fetchall()

        channels: dict[int, dict[str, Any]] = {}
        for row in rows:
            (
                source_id,
                source_kind,
                source_reference,
                source_title,
                avatar_url,
                poster_item_id,
                source_state,
                source_safety_verdict,
                item_id,
                video_id,
                item_title,
                channel_id,
                channel_title,
                item_state,
                thumbnail_url,
                backlog_status,
                candidate_json,
                backlog_quality_height,
                backlog_expires_at,
            ) = row
            if not _kids_channel_avatar_is_proxyable(avatar_url):
                continue
            item = {
                "id": item_id,
                "state": item_state,
                "video_id": video_id,
                "title": item_title,
                "channel_id": channel_id,
                "channel_title": channel_title,
            }
            source = {
                "kind": source_kind,
                "reference": source_reference,
                "title": source_title,
                "state": source_state,
                "safety_verdict": source_safety_verdict,
            }
            if (
                not _catalog_item_is_authorized(item, source)
                or backlog_status != "ready"
                or not _stored_candidate_meets_policy(
                    candidate_json,
                    backlog_quality_height,
                    minimum_quality_height,
                )
                or _parse_utc(backlog_expires_at) is None
                or _parse_utc(backlog_expires_at) <= expires_after
            ):
                continue
            channel = channels.setdefault(
                int(source_id),
                {
                    "source_id": int(source_id),
                    "kind": source_kind,
                    "reference": source_reference,
                    "title": str(source_title or "").strip(),
                    "avatar_url": str(avatar_url or "").strip(),
                    "poster_item_id": (
                        int(poster_item_id) if poster_item_id is not None else None
                    ),
                },
            )

        poster_items_by_source = await self._kids_source_poster_items_bulk(
            list(channels)
        )
        result: list[dict[str, Any]] = []
        for channel in channels.values():
            poster = _kids_effective_poster_item(
                channel["poster_item_id"],
                poster_items_by_source.get(channel["source_id"], []),
            )
            channel["poster_item"] = (
                {
                    "item_id": poster["id"],
                    "thumbnail_url": poster["thumbnail_url"],
                }
                if poster
                else None
            )
            result.append(channel)
        secrets.SystemRandom().shuffle(result)
        return result

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
                    SELECT profile,catalog_revision,policy_version,expires_at,source_id
                    FROM feed_sessions WHERE id=?
                    """,
                    (session_id,),
                )
            ).fetchone()
            if not session:
                return {"status": "not_found"}
            (
                session_profile,
                session_revision,
                session_policy,
                session_expires,
                session_source_id,
            ) = session
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
                      AND EXISTS (
                          SELECT 1 FROM catalog_source_profiles ps
                          WHERE ps.source_id=i.source_id AND ps.profile_slug=?
                      )
                      AND (? IS NULL OR i.source_id=?)
                      AND b.status='ready' AND b.expires_at>?
                    ORDER BY f.ordinal ASC
                    LIMIT ?
                    """,
                    (
                        session_id,
                        scan_ordinal,
                        KIDS_HOME_SOURCE_REFERENCE,
                        profile,
                        session_source_id,
                        session_source_id,
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
        profile: str | None = None,
        require_current_authorization: bool = True,
        minimum_remaining_seconds: int = 0,
        minimum_quality_height: int = 720,
    ) -> dict[str, Any] | None:
        minimum_quality_height = _quality_height_or_default(minimum_quality_height)
        now = datetime.now(timezone.utc)
        profile_filter = ""
        profile_args: tuple[Any, ...] = ()
        if profile:
            profile_filter = " AND fs.profile=?"
            profile_args = (profile,)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                f"""
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
                  AND EXISTS (
                      SELECT 1 FROM catalog_source_profiles ps
                      WHERE ps.source_id=i.source_id AND ps.profile_slug=fs.profile
                  )
                  {profile_filter}
                """,
                (
                    asset_id,
                    (now + timedelta(seconds=max(0, int(minimum_remaining_seconds)))).isoformat(),
                    *profile_args,
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
                  AND EXISTS (
                      SELECT 1 FROM catalog_source_profiles ps
                      WHERE ps.source_id=i.source_id AND ps.profile_slug=fs.profile
                  )
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
                       fs.profile,
                       i.video_id,i.title,i.channel_id,i.channel_title,i.state AS item_state,
                       s.kind AS _source_kind,s.reference AS _source_reference,
                       s.title AS _source_title,s.state AS _source_state,
                       s.safety_verdict AS _source_safety_verdict
                FROM relay_leases l
                JOIN feed_sessions fs ON fs.id=l.feed_session_id
                JOIN catalog_items i ON i.id=l.item_id
                JOIN catalog_sources s ON s.id=i.source_id
                WHERE l.id=?
                  AND EXISTS (
                      SELECT 1 FROM catalog_source_profiles ps
                      WHERE ps.source_id=i.source_id AND ps.profile_slug=fs.profile
                  )
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
                    SELECT item_id,video_id,priority,due,
                           ROW_NUMBER() OVER (
                               PARTITION BY priority,source_id
                               ORDER BY due ASC,item_id ASC
                           ) AS source_position
                    FROM (
                        SELECT b.item_id,b.video_id,i.source_id,0 AS priority,
                               COALESCE(b.next_attempt_at,'') AS due
                        FROM kids_resolve_backlog b
                        JOIN catalog_items i ON i.id=b.item_id
                        WHERE b.status='pending'
                          AND (b.next_attempt_at IS NULL OR b.next_attempt_at<=?)
                        UNION ALL
                        SELECT b.item_id,b.video_id,i.source_id,1 AS priority,
                               COALESCE(b.next_attempt_at,'') AS due
                        FROM kids_resolve_backlog b
                        JOIN catalog_items i ON i.id=b.item_id
                        WHERE b.status='retry'
                          AND (b.next_attempt_at IS NULL OR b.next_attempt_at<=?)
                        UNION ALL
                        SELECT b.item_id,b.video_id,i.source_id,2 AS priority,b.expires_at AS due
                        FROM kids_resolve_backlog b
                        JOIN catalog_items i ON i.id=b.item_id
                        WHERE b.status='ready' AND b.expires_at<=?
                    )
                )
                ORDER BY priority ASC,source_position ASC,due ASC,item_id ASC
                LIMIT ?
                """,
                (now_iso, now_iso, refresh_at, bounded),
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

    async def kids_resolve_summary(
        self,
        *,
        minimum_remaining_seconds: int = 300,
        profile: str | None = None,
    ) -> dict[str, Any]:
        now = (datetime.now(timezone.utc) + timedelta(seconds=max(0, minimum_remaining_seconds))).isoformat()
        async with aiosqlite.connect(self.db_path) as db:
            profile_filter = ""
            args: list[Any] = []
            if profile:
                profile_filter = """
                    AND EXISTS (
                        SELECT 1 FROM catalog_source_profiles ps
                        WHERE ps.source_id=i.source_id AND ps.profile_slug=?
                    )
                """
                args.append(profile)
            cur = await db.execute(
                f"""
                SELECT b.status,COUNT(*)
                FROM kids_resolve_backlog b
                JOIN catalog_items i ON i.id=b.item_id
                WHERE 1=1 {profile_filter}
                GROUP BY b.status
                """,
                tuple(args),
            )
            counts = {str(status): int(count) for status, count in await cur.fetchall()}
            row = await (
                await db.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM kids_resolve_backlog b
                    JOIN catalog_items i ON i.id=b.item_id
                    WHERE b.status='ready' AND b.expires_at>? {profile_filter}
                    """,
                    tuple([now, *args]),
                )
            ).fetchone()
        counts.setdefault("failed", counts.get("retry", 0))
        return {"counts": counts, "fresh_ready": int(row[0] if row else 0)}

    async def kids_resolve_recent_rows(
        self,
        limit: int = 20,
        *,
        status: str | None = None,
        profile: str | None = None,
        query: str | None = None,
        sort: str = "updated-desc",
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        status_value = str(status or "").strip().lower()
        if status_value == "failed":
            status_value = "retry"
        if status_value not in {"pending", "running", "ready", "retry", "blocked"}:
            status_value = ""
        sort_key = str(sort or "updated-desc").strip().lower()
        sort_desc = sort_key.endswith("-desc") or sort_key in {"updated", "expiry", "attempts", "quality"}
        sort_base = sort_key.rsplit("-", 1)[0] if sort_key.endswith(("-asc", "-desc")) else sort_key
        order_by = {
            "updated": "b.updated_at",
            "expiry": "COALESCE(b.expires_at,'9999-12-31T23:59:59+00:00')",
            "attempts": "b.attempt_count",
            "quality": "COALESCE(b.quality_height,0)",
            "title": "LOWER(COALESCE(i.title,''))",
            "status": "b.status",
            "id": "b.item_id",
        }.get(sort_base, "b.updated_at")
        order_direction = "DESC" if sort_desc else "ASC"
        where = []
        args: list[Any] = []
        if status_value:
            where.append("b.status=?")
            args.append(status_value)
        if profile:
            where.append(
                """
                EXISTS (
                    SELECT 1 FROM catalog_source_profiles ps
                    WHERE ps.source_id=i.source_id AND ps.profile_slug=?
                )
                """
            )
            args.append(profile)
        if query:
            escaped = (
                str(query).strip().casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            if escaped:
                where.append(
                    """
                    LOWER(
                        COALESCE(b.video_id,'') || ' ' ||
                        COALESCE(i.title,'') || ' ' ||
                        COALESCE(i.channel_title,'') || ' ' ||
                        COALESCE(s.title,'') || ' ' ||
                        COALESCE(b.last_error_code,'')
                    ) LIKE ? ESCAPE CHAR(92)
                    """
                )
                args.append(f"%{escaped}%")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                f"""
                SELECT b.item_id,b.video_id,i.title,i.channel_id,i.channel_title,
                       i.thumbnail_url,i.duration_seconds,i.visual_category,i.state AS item_state,
                       b.status,b.quality_height,b.codec,b.resolved_at,b.expires_at,
                       b.attempt_count,b.next_attempt_at,b.last_error_code,b.updated_at,
                       s.id AS source_id,s.kind AS source_kind,s.title AS source_title,
                       s.reference AS source_reference,s.state AS source_state,
                       s.safety_verdict AS source_safety_verdict,s.language AS source_language,
                       s.avatar_url,
                       COALESCE((
                           SELECT thumbnail_url
                           FROM catalog_items icon_item
                           WHERE icon_item.source_id=s.id AND trim(coalesce(icon_item.thumbnail_url,''))!=''
                           ORDER BY icon_item.id DESC LIMIT 1
                       ), '') AS fallback_avatar_url,
                       COALESCE((
                           SELECT GROUP_CONCAT(ps.profile_slug, ',')
                           FROM catalog_source_profiles ps
                           WHERE ps.source_id=s.id
                       ), '') AS profile_slugs
                FROM kids_resolve_backlog b
                JOIN catalog_items i ON i.id=b.item_id
                LEFT JOIN catalog_sources s ON s.id=i.source_id
                {where_sql}
                ORDER BY {order_by} {order_direction},b.item_id ASC
                LIMIT ?
                """,
                (*args, bounded),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        profile_names = {
            profile["slug"]: profile["display_name"]
            for profile in await self.kids_profiles_list()
        }
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(zip(cols, row))
            profiles = [
                value for value in str(item.pop("profile_slugs") or "").split(",") if value
            ]
            video_id = str(item.get("video_id") or "")
            channel_id = str(item.get("channel_id") or item.get("source_reference") or "")
            item["profile_slugs"] = profiles
            item["display_status"] = "failed" if item["status"] == "retry" else item["status"]
            item["video_url"] = kids_video_url(video_id)
            item["youtube_video_url"] = (
                f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
            )
            item["channel_url"] = kids_source_url("channel", channel_id)
            item["youtube_channel_url"] = (
                f"https://www.youtube.com/channel/{channel_id}" if channel_id.startswith("UC") else ""
            )
            item["source_url"] = kids_source_url(item.get("source_kind"), item.get("source_reference"))
            item["youtube_source_url"] = _youtube_source_url(
                item.get("source_kind"), item.get("source_reference")
            )
            item["avatar_url"] = item.get("avatar_url") or item.pop("fallback_avatar_url", "")
            item["profile_names"] = [
                profile_names[profile] for profile in profiles if profile in profile_names
            ]
            item["candidate_present"] = item["status"] == "ready" and bool(item.get("quality_height"))
            result.append(item)
        return result

    async def kids_playback_authorization(
        self,
        video_id: str,
        *,
        profile: str = "noah",
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
                  AND EXISTS (
                      SELECT 1 FROM catalog_source_profiles ps
                      WHERE ps.source_id=s.id AND ps.profile_slug=?
                  )
                  AND b.status='ready' AND b.quality_height>=? AND b.expires_at>?
                """,
                (
                    video_id,
                    KIDS_HOME_SOURCE_REFERENCE,
                    profile,
                    minimum_quality_height,
                    expires_after,
                ),
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

    async def kids_playback_policy_authorization(
        self,
        video_id: str,
        *,
        profile: str = "noah",
    ) -> dict[str, Any] | None:
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
                  AND EXISTS (
                      SELECT 1 FROM catalog_source_profiles ps
                      WHERE ps.source_id=s.id AND ps.profile_slug=?
                  )
                """,
                (video_id, KIDS_HOME_SOURCE_REFERENCE, profile),
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

    async def catalog_item_list_all(
        self,
        *,
        profile: str | None = None,
        state: str | None = None,
        query: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 1000))
        where: list[str] = []
        args: list[Any] = []
        if profile:
            where.append(
                """
                EXISTS (
                    SELECT 1 FROM catalog_source_profiles ps
                    WHERE ps.source_id=i.source_id AND ps.profile_slug=?
                )
                """
            )
            args.append(profile)
        if state in {"candidate", "approved", "blocked", "revoked", "unknown"}:
            where.append("i.state=?")
            args.append(state)
        if query:
            escaped = (
                str(query).strip().casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            if escaped:
                where.append(
                    """
                    LOWER(
                        COALESCE(i.video_id,'') || ' ' ||
                        COALESCE(i.title,'') || ' ' ||
                        COALESCE(i.channel_title,'') || ' ' ||
                        COALESCE(s.title,'')
                    ) LIKE ? ESCAPE CHAR(92)
                    """
                )
                args.append(f"%{escaped}%")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                f"""
                SELECT i.id,i.video_id,i.title,i.source_id,i.channel_id,i.channel_title,
                       i.thumbnail_url,i.duration_seconds,i.visual_category,i.state,
                       i.actor,i.changed_at,i.reason,i.revision,i.correlation_id,
                       s.kind AS source_kind,s.reference AS source_reference,
                       s.title AS source_title,s.state AS source_state,
                       s.safety_verdict AS source_safety_verdict,s.language AS source_language,
                       s.avatar_url,
                       b.status AS resolver_status,b.quality_height,b.codec,
                       b.resolved_at,b.expires_at,b.attempt_count,
                       b.next_attempt_at,b.last_error_code,b.updated_at AS resolver_updated_at,
                       COALESCE((
                           SELECT GROUP_CONCAT(ps.profile_slug, ',')
                           FROM catalog_source_profiles ps
                           WHERE ps.source_id=i.source_id
                       ), '') AS profile_slugs
                FROM catalog_items i
                LEFT JOIN catalog_sources s ON s.id=i.source_id
                LEFT JOIN kids_resolve_backlog b ON b.item_id=i.id
                {where_sql}
                ORDER BY i.id ASC
                LIMIT ?
                """,
                (*args, bounded),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        profile_names = {
            profile["slug"]: profile["display_name"]
            for profile in await self.kids_profiles_list()
        }
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(zip(cols, row))
            profiles = _profile_slugs(item.pop("profile_slugs"))
            video_id = str(item.get("video_id") or "")
            source_reference = str(item.get("source_reference") or "")
            source_kind = item.get("source_kind")
            item["profile_slugs"] = profiles
            item["profile_names"] = [
                profile_names[profile] for profile in profiles if profile in profile_names
            ]
            item["video_url"] = kids_video_url(video_id)
            item["youtube_video_url"] = (
                f"https://www.youtube.com/watch?v={quote(video_id, safe='')}" if video_id else ""
            )
            item["source_url"] = kids_source_url(source_kind, source_reference)
            item["youtube_source_url"] = _youtube_source_url(source_kind, source_reference)
            item["channel_url"] = kids_source_url("channel", item.get("channel_id"))
            item["youtube_channel_url"] = (
                f"https://www.youtube.com/channel/{quote(str(item['channel_id']), safe='')}"
                if str(item.get("channel_id") or "").startswith("UC")
                else ""
            )
            item["display_resolver_status"] = (
                "failed" if item.get("resolver_status") == "retry"
                else item.get("resolver_status") or "not_queued"
            )
            result.append(item)
        return result

    async def catalog_item_by_video(self, video_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("SELECT * FROM catalog_items WHERE video_id=?", (video_id,))
            row = await cur.fetchone()
            cols = [d[0] for d in cur.description] if row else []
        return dict(zip(cols, row)) if row else None

    async def catalog_sources_list(
        self,
        *,
        state: str | None = None,
        verdict: str | None = None,
        kind: str | None = None,
        profile: str | None = None,
        query: str | None = None,
        sort: str = "id-asc",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 1000))
        where: list[str] = []
        args: list[Any] = []
        if state in {"candidate", "approved", "blocked", "revoked", "unknown"}:
            where.append("s.state=?")
            args.append(state)
        if verdict:
            verdict_value = str(verdict).strip().upper()
            if verdict_value == "BLOCK":
                verdict_value = "UNSAFE"
            if verdict_value in {"SAFE", "UNSAFE", "UNCERTAIN"}:
                where.append("s.safety_verdict=?")
                args.append(verdict_value)
        if kind in {"channel", "playlist"}:
            where.append("s.kind=?")
            args.append(kind)
        if profile:
            where.append(
                """
                EXISTS (
                    SELECT 1 FROM catalog_source_profiles ps
                    WHERE ps.source_id=s.id AND ps.profile_slug=?
                )
                """
            )
            args.append(profile)
        if query:
            escaped = (
                str(query).strip().casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            if escaped:
                where.append(
                    """
                    LOWER(
                        COALESCE(s.title,'') || ' ' ||
                        COALESCE(s.reference,'') || ' ' ||
                        COALESCE(s.safety_reason,'')
                    ) LIKE ? ESCAPE CHAR(92)
                    """
                )
                args.append(f"%{escaped}%")
        order_key = str(sort or "id-asc").strip().lower()
        descending = order_key.endswith("-desc")
        order_base = order_key.rsplit("-", 1)[0] if order_key.endswith(("-asc", "-desc")) else order_key
        order_by = {
            "id": "s.id",
            "name": "LOWER(COALESCE(s.title,''))",
            "verdict": "s.safety_verdict",
            "samples": "s.safety_sample_count",
            "items": "item_count",
            "ready": "ready_item_count",
            "state": "s.state",
            "updated": "s.changed_at",
        }.get(order_base, "s.id")
        order_direction = "DESC" if descending else "ASC"
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                f"""
                SELECT s.*,
                       COUNT(DISTINCT i.id) AS item_count,
                       COUNT(DISTINCT CASE WHEN i.state='approved' THEN i.id END) AS approved_item_count,
                       COUNT(DISTINCT CASE WHEN b.status='ready' THEN i.id END) AS ready_item_count,
                       COALESCE((
                           SELECT GROUP_CONCAT(ps.profile_slug, ',')
                           FROM catalog_source_profiles ps
                           WHERE ps.source_id=s.id
                       ), '') AS profile_slugs,
                       COALESCE((
                           SELECT thumbnail_url
                           FROM catalog_items icon_item
                           WHERE icon_item.source_id=s.id AND trim(coalesce(icon_item.thumbnail_url,''))!=''
                           ORDER BY icon_item.id DESC LIMIT 1
                       ), '') AS fallback_avatar_url
                FROM catalog_sources s
                LEFT JOIN catalog_items i ON i.source_id=s.id
                LEFT JOIN kids_resolve_backlog b ON b.item_id=i.id
                {where_sql}
                GROUP BY s.id
                ORDER BY {order_by} {order_direction},s.id ASC
                LIMIT ?
                """,
                (*args, bounded),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        source_ids = [int(dict(zip(cols, row))["id"]) for row in rows]
        poster_items_by_source = await self._kids_source_poster_items_bulk(source_ids)
        profile_names = {
            profile["slug"]: profile["display_name"]
            for profile in await self.kids_profiles_list()
        }
        result: list[dict[str, Any]] = []
        for row in rows:
            source = dict(zip(cols, row))
            profiles = _profile_slugs(source.pop("profile_slugs"))
            source["profile_slugs"] = profiles
            source["profile_names"] = [
                profile_names[profile] for profile in profiles if profile in profile_names
            ]
            source["source_url"] = kids_source_url(source.get("kind"), source.get("reference"))
            source["youtube_source_url"] = _youtube_source_url(
                source.get("kind"), source.get("reference")
            )
            source["poster_item_id"] = (
                int(source["poster_item_id"])
                if source.get("poster_item_id") is not None
                else None
            )
            genuine_avatar_url = str(source.get("avatar_url") or "").strip()
            source["genuine_avatar_url"] = (
                genuine_avatar_url
                if _kids_channel_avatar_is_proxyable(genuine_avatar_url)
                else ""
            )
            fallback_avatar_url = str(
                source.get("fallback_avatar_url", "") or ""
            ).strip()
            source["fallback_avatar_url"] = fallback_avatar_url
            source["avatar_url"] = source["genuine_avatar_url"] or fallback_avatar_url
            effective = (
                _kids_effective_poster_item(
                    source["poster_item_id"],
                    poster_items_by_source.get(int(source["id"]), []),
                )
                if _kids_source_can_publish_poster(source)
                else None
            )
            source["effective_poster_thumbnail_url"] = (
                effective["thumbnail_url"] if effective else ""
            )
            result.append(source)
        return result

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

    async def kids_watch_events_list(
        self,
        limit: int = 100,
        *,
        profile: str | None = None,
        query: str | None = None,
        sort: str = "id-desc",
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 500))
        where: list[str] = []
        args: list[Any] = []
        if profile:
            where.append("w.profile=?")
            args.append(profile)
        if query:
            escaped = (
                str(query).strip().casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            if escaped:
                where.append(
                    """
                    LOWER(
                        COALESCE(w.video_id,'') || ' ' ||
                        COALESCE(i.title,'') || ' ' ||
                        COALESCE(s.title,'') || ' ' ||
                        COALESCE(w.event,'')
                    ) LIKE ? ESCAPE CHAR(92)
                    """
                )
                args.append(f"%{escaped}%")
        order_key = str(sort or "id-desc").strip().lower()
        descending = order_key.endswith("-desc") or order_key in {"id", "time"}
        order_base = order_key.rsplit("-", 1)[0] if order_key.endswith(("-asc", "-desc")) else order_key
        order_by = {
            "id": "w.id",
            "time": "w.created_at",
            "profile": "w.profile",
            "event": "w.event",
            "video": "LOWER(w.video_id)",
            "title": "LOWER(COALESCE(i.title,''))",
        }.get(order_base, "w.id")
        order_direction = "DESC" if descending else "ASC"
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                f"""
                SELECT w.*,i.title,i.source_id,i.thumbnail_url,i.channel_id,i.channel_title,
                       s.kind AS source_kind,s.reference AS source_reference,
                       s.title AS source_title,s.avatar_url
                FROM kids_watch_events w
                LEFT JOIN catalog_items i ON i.video_id=w.video_id
                LEFT JOIN catalog_sources s ON s.id=i.source_id
                {where_sql}
                ORDER BY {order_by} {order_direction},w.id DESC
                LIMIT ?
                """,
                (*args, bounded),
            )
            rows = await cur.fetchall()
            cols = [d[0] for d in cur.description]
        result: list[dict[str, Any]] = []
        for row in rows:
            event = dict(zip(cols, row))
            video_id = str(event.get("video_id") or "")
            event["video_url"] = kids_video_url(video_id)
            event["youtube_video_url"] = (
                f"https://www.youtube.com/watch?v={quote(video_id, safe='')}" if video_id else ""
            )
            event["source_url"] = kids_source_url(
                event.get("source_kind"), event.get("source_reference")
            )
            event["youtube_source_url"] = _youtube_source_url(
                event.get("source_kind"), event.get("source_reference")
            )
            event["channel_url"] = kids_source_url("channel", event.get("channel_id"))
            result.append(event)
        return result
