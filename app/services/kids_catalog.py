from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, quote, urlparse, urlsplit


KIDS_HOME_SOURCE_REFERENCE = "__youtube_kids_home__"
KIDS_PRACTICAL_CANDIDATE_TTL = timedelta(hours=5)
KIDS_SIGNED_URL_MARGIN = timedelta(minutes=15)
KIDS_MIN_RELAY_LIFETIME = timedelta(seconds=120)


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


def kids_source_url(kind: Any, reference: Any) -> str:
    """Return a public YouTube Kids URL for parent-facing links only."""
    raw = str(reference or "").strip()
    if raw == KIDS_HOME_SOURCE_REFERENCE:
        return "https://www.youtubekids.com/"
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.hostname != "www.youtubekids.com":
            return ""
        return raw
    if str(kind or "") == "channel" and raw:
        return f"https://www.youtubekids.com/channel/{quote(raw, safe='')}"
    if str(kind or "") == "playlist" and raw:
        return f"https://www.youtubekids.com/playlist?list={quote(raw, safe='')}"
    return ""


def kids_video_url(video_id: Any) -> str:
    value = str(video_id or "").strip()
    return (
        f"https://www.youtubekids.com/watch?v={quote(value, safe='')}"
        if value
        else ""
    )


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
    expiries = [_signed_stream_expiry(media_url), _signed_stream_expiry(audio_url)]
    if any(expiry is None for expiry in expiries):
        return False
    usable_after = (
        datetime.now(timezone.utc)
        + KIDS_MIN_RELAY_LIFETIME
        + KIDS_SIGNED_URL_MARGIN
    )
    return (
        type(candidate.get("quality_height")) is int
        and candidate["quality_height"] == quality_height
        and isinstance(media_url, str)
        and bool(media_url)
        and isinstance(audio_url, str)
        and bool(audio_url)
        and media_url != audio_url
        and all(expiry > usable_after for expiry in expiries if expiry is not None)
    )


def _signed_stream_expiry(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)
    if (
        parsed.scheme != "https"
        or (host != "googlevideo.com" and not host.endswith(".googlevideo.com"))
        or not query.get("expire")
        or not any(query.get(key) for key in ("sig", "signature", "lsig"))
    ):
        return None
    try:
        return datetime.fromtimestamp(int(query["expire"][0]), timezone.utc)
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return None


def kids_candidate_usable_until(
    candidate: Any,
    *,
    now: datetime,
    resolved_at: datetime,
) -> datetime | None:
    if not isinstance(candidate, dict):
        return None
    expiries = [
        _signed_stream_expiry(candidate.get("media_url")),
        _signed_stream_expiry(candidate.get("audio_url")),
    ]
    if any(expiry is None for expiry in expiries):
        return None
    current = (
        now.replace(tzinfo=timezone.utc)
        if now.tzinfo is None
        else now.astimezone(timezone.utc)
    )
    resolved = (
        resolved_at.replace(tzinfo=timezone.utc)
        if resolved_at.tzinfo is None
        else resolved_at.astimezone(timezone.utc)
    )
    usable_until = min(
        resolved + KIDS_PRACTICAL_CANDIDATE_TTL,
        *(expiry - KIDS_SIGNED_URL_MARGIN for expiry in expiries if expiry is not None),
    )
    return (
        usable_until
        if usable_until >= current + KIDS_MIN_RELAY_LIFETIME
        else None
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
