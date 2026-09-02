from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


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
    return channel_id == expected_channel_id


def _catalog_item_is_authorized(item: dict[str, Any], source: dict[str, Any]) -> bool:
    return not (
        item.get("state") != "approved"
        or source.get("state") != "approved"
        or source.get("safety_verdict") != "SAFE"
        or source.get("reference") == KIDS_HOME_SOURCE_REFERENCE
        or not _catalog_identity_is_known(item, source)
    )


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


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
