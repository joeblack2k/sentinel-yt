from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, quote, urlparse, urlsplit


KIDS_HOME_SOURCE_REFERENCE = "__youtube_kids_home__"
DEFAULT_KIDS_ITEM_POLICY_VERSION = "kids-item-safety-v1"
DEFAULT_KIDS_ITEM_RECHECK_SECONDS = 0
KIDS_PRACTICAL_CANDIDATE_TTL = timedelta(hours=5)
KIDS_SIGNED_URL_MARGIN = timedelta(minutes=15)
KIDS_MIN_RELAY_LIFETIME = timedelta(seconds=120)
KIDS_ITEM_LANGUAGES = frozenset({"nl", "en", "mixed", "unknown"})
KIDS_ITEM_VERDICTS = frozenset({"SAFE", "UNSAFE", "UNCERTAIN"})
KIDS_ITEM_CONTENT_KINDS = frozenset({"learning", "entertainment", "mixed", "unknown"})
KIDS_AGE_SUITABILITY_VALUES = frozenset({"SUITABLE", "UNSUITABLE", "UNCERTAIN"})
KIDS_PROFILE_SHELF_IDS = {
    "noah": ("new", "lego-build", "learning-nl", "fun-en", "fun-nl", "again"),
    "felix": ("new", "learning-nl", "fun-nl", "again"),
}
KIDS_SHELF_TARGET = 72
KIDS_SHELF_MIN_AGAIN = 1


def normalize_age_suitability(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"2", "6"}:
        raise ValueError("invalid age suitability")
    if any(
        not isinstance(value[age], str)
        or value[age] not in KIDS_AGE_SUITABILITY_VALUES
        for age in ("2", "6")
    ):
        raise ValueError("invalid age suitability")
    return {age: value[age] for age in ("2", "6")}


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


def catalog_item_input_hash(item: dict[str, Any]) -> str:
    values = [
        str(item.get(field) or "")
        for field in (
            "video_id",
            "title",
            "channel_id",
            "channel_title",
            "thumbnail_url",
        )
    ]
    canonical = json.dumps(values, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _catalog_item_is_authorized(item: dict[str, Any], source: dict[str, Any]) -> bool:
    return not (
        item.get("state") != "approved"
        or source.get("state") != "approved"
        or source.get("safety_verdict") != "SAFE"
        or source.get("reference") == KIDS_HOME_SOURCE_REFERENCE
        or not _catalog_identity_is_known(item, source)
    )


def _age_suitability(value: Any) -> dict[str, str] | None:
    try:
        return normalize_age_suitability(json.loads(str(value or "{}")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _source_is_authorized_for_profile(
    source: dict[str, Any],
    profile: str,
) -> bool:
    ages = _age_suitability(source.get("age_suitability_json"))
    language = str(source.get("language") or "unknown").strip().lower()
    return bool(
        source.get("safety_verdict") == "SAFE"
        and ages is not None
        and (
            profile == "felix"
            and language in {"nl", "mixed"}
            and ages.get("2") == "SUITABLE"
            or profile == "noah"
            and language in {"nl", "en", "mixed"}
            and ages.get("6") == "SUITABLE"
        )
    )


def _catalog_item_safety_is_current(
    item: dict[str, Any],
    *,
    policy_version: str,
    recheck_seconds: int,
    now: datetime | None = None,
) -> bool:
    if (
        not isinstance(policy_version, str)
        or not policy_version
        or item.get("safety_policy_version") != policy_version
        or item.get("safety_input_hash") != catalog_item_input_hash(item)
        or item.get("safety_verdict") not in KIDS_ITEM_VERDICTS
        or str(item.get("language") or "").strip().lower()
        not in KIDS_ITEM_LANGUAGES
        or _age_suitability(item.get("age_suitability_json")) is None
    ):
        return False
    checked_at_value = item.get("safety_checked_at")
    if not isinstance(checked_at_value, str) or not checked_at_value:
        return False
    try:
        parsed_checked_at = datetime.fromisoformat(checked_at_value)
    except ValueError:
        return False
    if parsed_checked_at.tzinfo is None:
        return False
    checked_at = parsed_checked_at.astimezone(timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    try:
        window_seconds = max(0, int(recheck_seconds))
    except (TypeError, ValueError):
        return False
    return checked_at <= current and (
        window_seconds == 0
        or checked_at >= current - timedelta(seconds=window_seconds)
    )


def _catalog_item_is_authorized_for_profile(
    item: dict[str, Any],
    source: dict[str, Any],
    profile: str,
    *,
    policy_version: str,
    recheck_seconds: int = DEFAULT_KIDS_ITEM_RECHECK_SECONDS,
    now: datetime | None = None,
) -> bool:
    if not _catalog_item_is_authorized(item, source):
        return False
    profile = str(profile or "").strip().lower()
    if not _source_is_authorized_for_profile(source, profile):
        return False
    if not _catalog_item_safety_is_current(
        item,
        policy_version=policy_version,
        recheck_seconds=recheck_seconds,
        now=now,
    ):
        return False
    return _source_is_authorized_for_profile(item, profile)


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
        "language": values.pop("_source_language", None),
        "content_kind": values.pop("_source_content_kind", None),
        "age_suitability_json": values.pop("_source_age_suitability_json", None),
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


def _kids_shelf_language_rank(shelf: str, language: str) -> int | None:
    if shelf in {"again", "new"}:
        return 0
    if shelf in {"learning-nl", "fun-nl"}:
        return {"nl": 0, "mixed": 1}.get(language)
    if shelf == "fun-en":
        return {"en": 0, "mixed": 1}.get(language)
    return None


def _kids_editorial_allowed(item: dict[str, Any], profile: str, shelf: str | None) -> bool:
    if shelf == "again":
        return True
    try:
        editorial = json.loads(item.get("editorial_classification_json") or "null")
    except (ValueError, TypeError):
        editorial = None
    if not isinstance(editorial, dict) or editorial.get("input_hash") != catalog_item_input_hash(item):
        editorial = {}
    if profile == "noah" and editorial.get("target_audience") == "preschool":
        return False
    if shelf == "lego-build":
        return profile == "noah" and editorial.get("category") == "lego_build"
    if profile == "noah" and editorial.get("category") == "lego_build":
        return False
    return True


def _kids_shelf_candidate_allowed(
    item: dict[str, Any],
    *,
    profile: str,
    shelf: str,
) -> bool:
    if not _kids_editorial_allowed(item, profile, shelf):
        return False
    raw_completed_at = item.get("_profile_completed_at")
    completed_at = _parse_utc(raw_completed_at)
    if shelf == "again":
        return completed_at is not None
    if str(raw_completed_at or "").strip():
        return False
    if shelf == "lego-build":
        return True
    if shelf == "new":
        language = str(item.get("_source_language") or "unknown").strip().lower()
        return (
            language
            in ({"nl", "mixed"} if profile == "felix" else {"nl", "en", "mixed"})
        )
    language = str(item.get("_source_language") or "unknown").strip().lower()
    content_kind = str(item.get("_source_content_kind") or "unknown").strip().lower()
    if shelf == "learning-nl" and content_kind not in {"learning", "mixed"}:
        return False
    if shelf in {"fun-en", "fun-nl"} and content_kind not in {
        "entertainment",
        "mixed",
    }:
        return False
    return _kids_shelf_language_rank(shelf, language) is not None
