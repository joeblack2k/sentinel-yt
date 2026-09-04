#!/usr/bin/env python3
"""Merge the persistent Kids catalog from the former 8091 database into Sentinel.

The source is read-only. Existing Sentinel policy decisions win. Feed sessions and
relay leases are deliberately excluded because they are short-lived and revision
bound; 8090 creates fresh sessions after the merge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MIGRATION_KEY = "subtube-kids-to-sentinel-v1"
STATE_VALUES = {"candidate", "approved", "blocked", "revoked", "unknown"}
LANGUAGE_VALUES = {"nl", "en", "mixed", "unknown"}
CONTENT_KIND_VALUES = {"learning", "entertainment", "mixed", "unknown"}
CORE_TABLES = (
    "catalog_meta",
    "catalog_sources",
    "catalog_items",
    "catalog_transitions",
    "kids_audit_events",
    "kids_watch_events",
    "kids_resolve_backlog",
)
TARGET_TABLES = CORE_TABLES + (
    "kids_profiles",
    "catalog_source_profiles",
    "feed_sessions",
    "feed_session_items",
    "relay_leases",
)
EPHEMERAL_TABLES = ("feed_sessions", "feed_session_items", "relay_leases")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> str:
    return str(value or "").strip()


def _bounded(value: Any, limit: int) -> str:
    return _iso(value)[:limit]


def _state(value: Any) -> str:
    value = _iso(value).lower()
    return value if value in STATE_VALUES else "unknown"


def _language(value: Any) -> str:
    value = _iso(value).lower()
    return value if value in LANGUAGE_VALUES else "unknown"


def _content_kind(value: Any) -> str:
    value = _iso(value).lower()
    return value if value in CONTENT_KIND_VALUES else "unknown"


def _verdict(value: Any) -> str:
    value = _iso(value).upper()
    return value if value in {"SAFE", "UNSAFE", "UNCERTAIN"} else "UNCERTAIN"


def _rows(connection: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(query, params)]


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _fingerprint(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for table in (*CORE_TABLES, "settings"):
        if table not in _table_names(connection):
            continue
        columns = [
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})")
        ]
        order = ", ".join(columns[:1]) if columns else "rowid"
        digest.update(table.encode("utf-8"))
        digest.update(b"\0")
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            digest.update(
                json.dumps(list(row), ensure_ascii=True, separators=(",", ":"), default=str).encode(
                    "utf-8"
                )
            )
            digest.update(b"\n")
    return digest.hexdigest()


def _same_file(source: Path, destination: Path) -> bool:
    if not destination.exists():
        return source == destination.resolve()
    return source.samefile(destination)


def _valid_candidate(row: dict[str, Any], *, now: datetime) -> tuple[str, int, str, str, str] | None:
    if _iso(row.get("status")) != "ready":
        return None
    try:
        candidate = json.loads(_iso(row.get("candidate_json")))
        quality = int(row.get("quality_height"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(candidate, dict) or not 720 <= quality <= 1080:
        return None
    if candidate.get("quality_height") != quality:
        return None
    if candidate.get("kind") not in (None, "adaptive_mpv"):
        return None
    if not all(
        isinstance(candidate.get(key), str)
        and urlparse(candidate[key]).scheme in {"http", "https"}
        for key in ("media_url", "audio_url")
    ):
        return None
    if candidate["media_url"] == candidate["audio_url"]:
        return None
    if not isinstance(candidate.get("video_headers"), dict) or not isinstance(
        candidate.get("audio_headers"), dict
    ):
        return None
    try:
        expires_at = datetime.fromisoformat(_iso(row.get("expires_at")))
    except ValueError:
        return None
    if expires_at.tzinfo is None or expires_at <= now + timedelta(seconds=60):
        return None
    resolved_at = _iso(row.get("resolved_at"))
    if not resolved_at:
        return None
    return (
        json.dumps(candidate, ensure_ascii=True, separators=(",", ":")),
        quality,
        _bounded(row.get("codec"), 128),
        resolved_at,
        expires_at.astimezone(timezone.utc).isoformat(),
    )


def _candidate_status(row: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    status = _iso(row.get("status")).lower()
    if status == "blocked":
        return {
            "status": "blocked",
            "candidate_json": "",
            "quality_height": None,
            "codec": "",
            "resolved_at": None,
            "expires_at": None,
            "next_attempt_at": None,
            "last_error_code": _bounded(row.get("last_error_code"), 128),
        }
    candidate = _valid_candidate(row, now=now)
    if candidate is not None:
        candidate_json, quality, codec, resolved_at, expires_at = candidate
        return {
            "status": "ready",
            "candidate_json": candidate_json,
            "quality_height": quality,
            "codec": codec,
            "resolved_at": resolved_at,
            "expires_at": expires_at,
            "next_attempt_at": None,
            "last_error_code": "",
        }
    return {
        "status": "retry" if status in {"retry", "running"} else "pending",
        "candidate_json": "",
        "quality_height": None,
        "codec": "",
        "resolved_at": None,
        "expires_at": None,
        "next_attempt_at": _iso(row.get("next_attempt_at")) or None,
        "last_error_code": "migration_quality_recheck",
    }


def _target_candidate_is_live(row: sqlite3.Row | None, *, now: datetime) -> bool:
    if row is None:
        return False
    return _valid_candidate(dict(row), now=now) is not None


def _insert_if_missing(
    connection: sqlite3.Connection,
    query: str,
    params: tuple[Any, ...],
    exists_query: str,
    exists_params: tuple[Any, ...],
) -> bool:
    if connection.execute(exists_query, exists_params).fetchone():
        return False
    connection.execute(query, params)
    return True


def merge_kids_database(source_path: str | Path, destination_path: str | Path) -> dict[str, Any]:
    source_path = Path(source_path).expanduser().resolve(strict=True)
    destination_path = Path(destination_path).expanduser()
    if _same_file(source_path, destination_path):
        raise ValueError("source and destination must be different database files")

    source = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    source_tables = _table_names(source)
    missing_source = set(CORE_TABLES) - source_tables
    if missing_source:
        source.close()
        raise ValueError(f"source database is missing tables: {', '.join(sorted(missing_source))}")

    try:
        fingerprint = _fingerprint(source)
        destination = sqlite3.connect(destination_path)
        destination.row_factory = sqlite3.Row
        destination.execute("PRAGMA foreign_keys=ON")
        target_tables = _table_names(destination)
        missing_target = set(TARGET_TABLES) - target_tables
        if missing_target:
            destination.close()
            raise ValueError(
                "Sentinel database is not initialized for Kids dataplane: "
                + ", ".join(sorted(missing_target))
            )
        destination.execute(
            """
            CREATE TABLE IF NOT EXISTS kids_migration_runs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                migration_key TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL UNIQUE,
                applied_at TEXT NOT NULL,
                report_json TEXT NOT NULL
            )
            """
        )
        previous = destination.execute(
            """
            SELECT report_json FROM kids_migration_runs
            WHERE migration_key=? AND source_fingerprint=?
            """,
            (MIGRATION_KEY, fingerprint),
        ).fetchone()
        if previous:
            report = json.loads(str(previous["report_json"]))
            destination.close()
            return report

        now = _now()
        destination.execute("BEGIN IMMEDIATE")
        try:
            revision_row = destination.execute(
                "SELECT value FROM catalog_meta WHERE key='revision'"
            ).fetchone()
            current_revision = int(revision_row[0]) if revision_row else 0
            import_revision = current_revision + 1
            counts = {
                "sources_inserted": 0,
                "sources_existing": 0,
                "sources_enriched": 0,
                "items_inserted": 0,
                "items_existing": 0,
                "items_enriched": 0,
                "backlog_inserted": 0,
                "backlog_ready_imported": 0,
                "backlog_rechecked": 0,
                "transitions_imported": 0,
                "audit_events_imported": 0,
                "watch_events_imported": 0,
                "source_profile_assignments": 0,
                "profile_setting_imported": 0,
                "source_posters_imported": 0,
                "skipped_home_rows": 0,
            }

            source_map: dict[int, int] = {}
            for row in _rows(source, "SELECT * FROM catalog_sources ORDER BY id"):
                reference = _bounded(row.get("reference"), 256)
                if not reference or reference == "__youtube_kids_home__":
                    counts["skipped_home_rows"] += 1
                    continue
                existing = destination.execute(
                    "SELECT * FROM catalog_sources WHERE reference=?", (reference,)
                ).fetchone()
                if existing:
                    source_id = int(existing["id"])
                    source_map[int(row["id"])] = source_id
                    counts["sources_existing"] += 1
                    updates: dict[str, Any] = {}
                    if not _iso(existing["title"]) and _iso(row.get("title")):
                        updates["title"] = _bounded(row.get("title"), 500)
                    if _language(existing["language"]) == "unknown" and _language(row.get("language")) != "unknown":
                        updates["language"] = _language(row.get("language"))
                    if (
                        _content_kind(existing["content_kind"]) == "unknown"
                        and _content_kind(row.get("content_kind")) != "unknown"
                    ):
                        updates["content_kind"] = _content_kind(row.get("content_kind"))
                    if updates:
                        assignments = ", ".join(f"{key}=?" for key in updates)
                        destination.execute(
                            f"UPDATE catalog_sources SET {assignments} WHERE id=?",
                            (*updates.values(), source_id),
                        )
                        counts["sources_enriched"] += 1
                    continue
                cursor = destination.execute(
                    """
                    INSERT INTO catalog_sources(
                        kind,reference,title,language,content_kind,safety_verdict,safety_reason,
                        safety_checked_at,safety_policy_version,safety_evidence_json,
                        safety_sample_count,state,actor,changed_at,reason,revision,correlation_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        _bounded(row.get("kind"), 32) if _iso(row.get("kind")) in {"channel", "playlist"} else "channel",
                        reference,
                        _bounded(row.get("title"), 500),
                        _language(row.get("language")),
                        _content_kind(row.get("content_kind")),
                        _verdict(row.get("safety_verdict")),
                        _bounded(row.get("safety_reason"), 1000),
                        _iso(row.get("safety_checked_at")) or None,
                        _bounded(row.get("safety_policy_version"), 128),
                        _iso(row.get("safety_evidence_json")) or "[]",
                        max(0, int(row.get("safety_sample_count") or 0)),
                        _state(row.get("state")),
                        _bounded(row.get("actor"), 128) or "kids-migration",
                        _iso(row.get("changed_at")) or now.isoformat(),
                        _bounded(row.get("reason"), 1000) or "merged from Kids Guardian",
                        import_revision,
                        _bounded(row.get("correlation_id"), 128) or f"kids-merge-source-{row['id']}",
                    ),
                )
                source_map[int(row["id"])] = int(cursor.lastrowid)
                counts["sources_inserted"] += 1
                destination.execute(
                    """
                    INSERT OR IGNORE INTO catalog_source_profiles(
                        source_id,profile_slug,actor,reason,assigned_at
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        int(cursor.lastrowid),
                        "noah",
                        "kids-migration",
                        "Imported source retained for Noah",
                        now.isoformat(),
                    ),
                )
                counts["source_profile_assignments"] += 1

            item_map: dict[int, int] = {}
            for row in _rows(source, "SELECT * FROM catalog_items ORDER BY id"):
                mapped_source = source_map.get(int(row["source_id"])) if row.get("source_id") is not None else None
                if mapped_source is None:
                    counts["skipped_home_rows"] += 1
                    continue
                video_id = _bounded(row.get("video_id"), 64)
                if not video_id:
                    continue
                existing = destination.execute(
                    "SELECT * FROM catalog_items WHERE video_id=?", (video_id,)
                ).fetchone()
                if existing:
                    item_id = int(existing["id"])
                    item_map[int(row["id"])] = item_id
                    counts["items_existing"] += 1
                    updates: dict[str, Any] = {}
                    if not _iso(existing["title"]) and _iso(row.get("title")):
                        updates["title"] = _bounded(row.get("title"), 500)
                    if not _iso(existing["channel_id"]) and _iso(row.get("channel_id")):
                        updates["channel_id"] = _bounded(row.get("channel_id"), 128)
                    if not _iso(existing["channel_title"]) and _iso(row.get("channel_title")):
                        updates["channel_title"] = _bounded(row.get("channel_title"), 500)
                    if not _iso(existing["thumbnail_url"]) and _iso(row.get("thumbnail_url")):
                        updates["thumbnail_url"] = _bounded(row.get("thumbnail_url"), 2000)
                    if int(existing["duration_seconds"] or 0) == 0 and int(row.get("duration_seconds") or 0) > 0:
                        updates["duration_seconds"] = max(0, int(row["duration_seconds"]))
                    if _iso(existing["visual_category"]) in {"", "general"} and _iso(row.get("visual_category")) not in {"", "general"}:
                        updates["visual_category"] = _bounded(row.get("visual_category"), 64)
                    if updates:
                        assignments = ", ".join(f"{key}=?" for key in updates)
                        destination.execute(
                            f"UPDATE catalog_items SET {assignments} WHERE id=?",
                            (*updates.values(), item_id),
                        )
                        counts["items_enriched"] += 1
                    continue
                cursor = destination.execute(
                    """
                    INSERT INTO catalog_items(
                        video_id,title,source_id,channel_id,channel_title,thumbnail_url,
                        duration_seconds,visual_category,state,actor,changed_at,reason,
                        revision,correlation_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        video_id,
                        _bounded(row.get("title"), 500),
                        mapped_source,
                        _bounded(row.get("channel_id"), 128),
                        _bounded(row.get("channel_title"), 500),
                        _bounded(row.get("thumbnail_url"), 2000),
                        max(0, int(row.get("duration_seconds") or 0)),
                        _bounded(row.get("visual_category"), 64) or "general",
                        _state(row.get("state")),
                        _bounded(row.get("actor"), 128) or "kids-migration",
                        _iso(row.get("changed_at")) or now.isoformat(),
                        _bounded(row.get("reason"), 1000) or "merged from Kids Guardian",
                        import_revision,
                        _bounded(row.get("correlation_id"), 128) or f"kids-merge-item-{row['id']}",
                    ),
                )
                item_map[int(row["id"])] = int(cursor.lastrowid)
                counts["items_inserted"] += 1

            if "poster_item_id" in {
                str(column[1])
                for column in source.execute("PRAGMA table_info(catalog_sources)")
            }:
                for row in _rows(
                    source,
                    "SELECT id,poster_item_id FROM catalog_sources "
                    "WHERE poster_item_id IS NOT NULL ORDER BY id",
                ):
                    mapped_source = source_map.get(int(row["id"]))
                    mapped_poster = item_map.get(int(row["poster_item_id"]))
                    if mapped_source is None or mapped_poster is None:
                        continue
                    cursor = destination.execute(
                        "UPDATE catalog_sources SET poster_item_id=? "
                        "WHERE id=? AND poster_item_id IS NULL",
                        (mapped_poster, mapped_source),
                    )
                    counts["source_posters_imported"] += cursor.rowcount

            for row in _rows(source, "SELECT * FROM kids_resolve_backlog ORDER BY item_id"):
                target_item = item_map.get(int(row["item_id"]))
                if target_item is None:
                    continue
                desired = _candidate_status(row, now=now)
                existing = destination.execute(
                    "SELECT * FROM kids_resolve_backlog WHERE item_id=?", (target_item,)
                ).fetchone()
                if existing:
                    if existing["status"] == "blocked":
                        continue
                    if _target_candidate_is_live(existing, now=now):
                        continue
                    if desired["status"] == "ready":
                        destination.execute(
                            """
                            UPDATE kids_resolve_backlog
                            SET video_id=?,status=?,candidate_json=?,quality_height=?,codec=?,
                                resolved_at=?,expires_at=?,next_attempt_at=?,last_error_code=?,updated_at=?
                            WHERE item_id=?
                            """,
                            (
                                _bounded(row.get("video_id"), 64),
                                desired["status"],
                                desired["candidate_json"],
                                desired["quality_height"],
                                desired["codec"],
                                desired["resolved_at"],
                                desired["expires_at"],
                                desired["next_attempt_at"],
                                desired["last_error_code"],
                                now.isoformat(),
                                target_item,
                            ),
                        )
                        counts["backlog_ready_imported"] += 1
                    continue
                destination.execute(
                    """
                    INSERT INTO kids_resolve_backlog(
                        item_id,video_id,status,candidate_json,quality_height,codec,
                        resolved_at,expires_at,attempt_count,next_attempt_at,
                        last_error_code,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        target_item,
                        _bounded(row.get("video_id"), 64),
                        desired["status"],
                        desired["candidate_json"],
                        desired["quality_height"],
                        desired["codec"],
                        desired["resolved_at"],
                        desired["expires_at"],
                        max(0, int(row.get("attempt_count") or 0)),
                        desired["next_attempt_at"],
                        desired["last_error_code"],
                        now.isoformat(),
                    ),
                )
                counts["backlog_inserted"] += 1
                if desired["status"] == "ready":
                    counts["backlog_ready_imported"] += 1
                elif desired["last_error_code"] == "migration_quality_recheck":
                    counts["backlog_rechecked"] += 1

            for row in _rows(source, "SELECT * FROM catalog_transitions ORDER BY id"):
                entity_type = _iso(row.get("entity_type"))
                mapped_id = (
                    source_map.get(int(row["entity_id"]))
                    if entity_type == "source"
                    else item_map.get(int(row["entity_id"]))
                    if entity_type == "item"
                    else None
                )
                if mapped_id is None:
                    continue
                inserted = _insert_if_missing(
                    destination,
                    """
                    INSERT INTO catalog_transitions(
                        entity_type,entity_id,from_state,to_state,actor,changed_at,
                        reason,revision,correlation_id
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        entity_type,
                        mapped_id,
                        _state(row.get("from_state")),
                        _state(row.get("to_state")),
                        _bounded(row.get("actor"), 128),
                        _iso(row.get("changed_at")) or now.isoformat(),
                        _bounded(row.get("reason"), 1000),
                        max(0, int(row.get("revision") or 0)),
                        _bounded(row.get("correlation_id"), 128),
                    ),
                    """
                    SELECT 1 FROM catalog_transitions
                    WHERE entity_type=? AND entity_id=? AND from_state=? AND to_state=?
                      AND changed_at=? AND correlation_id=?
                    """,
                    (
                        entity_type,
                        mapped_id,
                        _state(row.get("from_state")),
                        _state(row.get("to_state")),
                        _iso(row.get("changed_at")) or now.isoformat(),
                        _bounded(row.get("correlation_id"), 128),
                    ),
                )
                counts["transitions_imported"] += int(inserted)

            for row in _rows(source, "SELECT * FROM kids_audit_events ORDER BY id"):
                entity_type = _iso(row.get("entity_type"))
                entity_id = row.get("entity_id")
                if entity_type == "source" and entity_id is not None:
                    entity_id = source_map.get(int(entity_id))
                elif entity_type == "item" and entity_id is not None:
                    entity_id = item_map.get(int(entity_id))
                if entity_type in {"source", "item"} and entity_id is None:
                    continue
                values = (
                    _bounded(row.get("event"), 128),
                    entity_type,
                    entity_id,
                    _bounded(row.get("actor"), 128),
                    _bounded(row.get("reason"), 1000),
                    max(0, int(row.get("revision") or 0)),
                    _bounded(row.get("correlation_id"), 128),
                    _iso(row.get("created_at")) or now.isoformat(),
                )
                inserted = _insert_if_missing(
                    destination,
                    """
                    INSERT INTO kids_audit_events(
                        event,entity_type,entity_id,actor,reason,revision,correlation_id,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    values,
                    """
                    SELECT 1 FROM kids_audit_events
                    WHERE event=? AND entity_type=? AND COALESCE(entity_id,-1)=COALESCE(?, -1)
                      AND actor=? AND reason=? AND revision=? AND correlation_id=? AND created_at=?
                    """,
                    values,
                )
                counts["audit_events_imported"] += int(inserted)

            for row in _rows(source, "SELECT * FROM kids_watch_events ORDER BY id"):
                values = (
                    _bounded(row.get("video_id"), 64),
                    _bounded(row.get("event"), 32),
                    _bounded(row.get("profile"), 64) or "noah",
                    row.get("position_seconds"),
                    _bounded(row.get("session_id"), 128),
                    row.get("startup_ms"),
                    _bounded(row.get("correlation_id"), 128),
                    _iso(row.get("created_at")) or now.isoformat(),
                )
                inserted = _insert_if_missing(
                    destination,
                    """
                    INSERT INTO kids_watch_events(
                        video_id,event,profile,position_seconds,session_id,startup_ms,
                        correlation_id,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    values,
                    """
                    SELECT 1 FROM kids_watch_events
                    WHERE video_id=? AND event=? AND profile=?
                      AND COALESCE(position_seconds,-1)=COALESCE(?, -1)
                      AND session_id=? AND COALESCE(startup_ms,-1)=COALESCE(?, -1)
                      AND correlation_id=? AND created_at=?
                    """,
                    values,
                )
                counts["watch_events_imported"] += int(inserted)

            if "settings" in source_tables:
                profile = source.execute(
                    "SELECT value FROM settings WHERE key='kids_profile_max_age'"
                ).fetchone()
                existing_profile = destination.execute(
                    "SELECT value FROM settings WHERE key='kids_profile_max_age'"
                ).fetchone()
                if profile and not existing_profile:
                    try:
                        age = int(str(profile[0]))
                    except ValueError:
                        age = -1
                    if 0 <= age <= 18:
                        destination.execute(
                            "INSERT INTO settings(key,value) VALUES('kids_profile_max_age',?)",
                            (str(age),),
                        )
                        counts["profile_setting_imported"] += 1

            destination.execute(
                """
                INSERT OR IGNORE INTO catalog_meta(key,value) VALUES('revision',?)
                """,
                (current_revision,),
            )
            if any(
                counts[key] > 0
                for key in (
                    "sources_inserted",
                    "items_inserted",
                    "backlog_inserted",
                    "backlog_ready_imported",
                    "transitions_imported",
                    "audit_events_imported",
                    "watch_events_imported",
                    "profile_setting_imported",
                )
            ):
                destination.execute(
                    "UPDATE catalog_meta SET value=? WHERE key='revision'",
                    (import_revision,),
                )
                destination.execute(
                    """
                    INSERT INTO kids_audit_events(
                        event,entity_type,entity_id,actor,reason,revision,correlation_id,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        "kids_data_merged",
                        "control",
                        None,
                        "kids-migration",
                        "Merged persistent catalog, resolver backlog, audit history, and watch history",
                        import_revision,
                        f"kids-merge-{fingerprint[:16]}",
                        now.isoformat(),
                    ),
                )
                counts["audit_events_imported"] += 1

            excluded = {
                table: int(source.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in EPHEMERAL_TABLES
                if table in source_tables
            }
            report = {
                "migration_key": MIGRATION_KEY,
                "source_path": str(source_path),
                "source_fingerprint": fingerprint,
                "target_revision_before": current_revision,
                "target_revision_after": import_revision
                if any(value > 0 for value in counts.values())
                else current_revision,
                "row_counts": counts,
                "excluded_ephemeral_rows": excluded,
                "note": "Fresh 8090 feed sessions and relay leases are created after migration.",
            }
            destination.execute(
                """
                INSERT INTO kids_migration_runs(
                    migration_key,source_path,source_fingerprint,applied_at,report_json
                ) VALUES(?,?,?,?,?)
                """,
                (
                    MIGRATION_KEY,
                    str(source_path),
                    fingerprint,
                    now.isoformat(),
                    json.dumps(report, ensure_ascii=True, sort_keys=True),
                ),
            )
            destination.commit()
            destination.close()
            return report
        except Exception:
            destination.rollback()
            destination.close()
            raise
    finally:
        source.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge persistent Kids data into Sentinel")
    parser.add_argument("source", help="read-only former 8091 SQLite database")
    parser.add_argument("destination", help="Sentinel SQLite database")
    args = parser.parse_args()
    print(
        json.dumps(
            merge_kids_database(args.source, args.destination),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
