from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from ..config import Settings
from ..db import Database


logger = logging.getLogger(__name__)

PRACTICAL_CANDIDATE_TTL = timedelta(minutes=20)
MIN_CANDIDATE_QUALITY_HEIGHT = 720
MAX_CANDIDATE_QUALITY_HEIGHT = 1080


def _utc_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _signed_googlevideo_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)
    return (
        parsed.scheme == "https"
        and (host == "googlevideo.com" or host.endswith(".googlevideo.com"))
        and bool(query.get("expire"))
        and any(query.get(key) for key in ("sig", "signature", "lsig"))
    )


def _signed_expiry(value: Any) -> datetime | None:
    if not _signed_googlevideo_url(value):
        return None
    try:
        expiry = datetime.fromtimestamp(int(parse_qs(urlsplit(value).query)["expire"][0]), timezone.utc)
    except (KeyError, ValueError, OSError, OverflowError):
        return None
    return expiry


def normalize_candidate(
    payload: Any,
    *,
    minimum_quality_height: int = MIN_CANDIDATE_QUALITY_HEIGHT,
) -> tuple[dict[str, Any], int, str, str, str] | None:
    if not isinstance(payload, dict) or payload.get("status") != "ready":
        return None
    if (
        type(minimum_quality_height) is not int
        or not MIN_CANDIDATE_QUALITY_HEIGHT <= minimum_quality_height <= MAX_CANDIDATE_QUALITY_HEIGHT
    ):
        return None
    candidate = payload.get("candidate")
    expires_at = _utc_timestamp(payload.get("expires_at"))
    resolved_at = _utc_timestamp(payload.get("resolved_at"))
    if not isinstance(candidate, dict) or not expires_at or not resolved_at:
        return None
    quality = candidate.get("quality_height")
    codec = candidate.get("codec")
    if (
        type(quality) is not int
        or not minimum_quality_height <= quality <= MAX_CANDIDATE_QUALITY_HEIGHT
        or not isinstance(codec, str)
        or not codec
    ):
        return None
    video_headers = candidate.get("video_headers")
    audio_headers = candidate.get("audio_headers")
    if not isinstance(video_headers, dict) or not isinstance(audio_headers, dict):
        return None
    kind = candidate.get("kind")
    if kind not in (None, "adaptive_mpv"):
        return None
    media_url = candidate.get("media_url")
    audio_url = candidate.get("audio_url")
    if not isinstance(media_url, str) or not isinstance(audio_url, str) or media_url == audio_url:
        return None
    expiries = [_signed_expiry(media_url), _signed_expiry(audio_url)]
    required = {"media_url", "audio_url", "quality_height", "codec", "video_headers", "audio_headers"}
    if any(expiry is None for expiry in expiries):
        return None
    resolved_datetime = datetime.fromisoformat(resolved_at)
    earliest_expiry = min(
        *(expiry for expiry in expiries if expiry is not None),
        resolved_datetime + PRACTICAL_CANDIDATE_TTL,
    )
    if earliest_expiry <= datetime.now(timezone.utc) + timedelta(seconds=120):
        return None
    if not required.issubset(candidate):
        return None
    return candidate, quality, codec, resolved_at, earliest_expiry.isoformat()


async def run_once(
    *,
    db: Database,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> dict[str, int]:
    await db.init()
    await db.kids_resolve_sync_backlog(
        minimum_quality_height=settings.kids_resolver_min_quality_height,
    )
    jobs = await db.kids_resolve_claim_due(
        limit=settings.kids_resolver_batch_size,
        refresh_margin_seconds=settings.kids_resolve_refresh_margin_seconds,
    )
    counts: dict[str, int] = {"claimed": len(jobs), "ready": 0, "retry": 0}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=20.0)
    try:
        for job in jobs:
            reason = "resolver_error"
            try:
                response = await client.post(
                    f"{settings.kids_resolver_backend_url}/api/youtube/videos/{job['video_id']}/resolve-adaptive",
                    params={"target_height": 1080, "transport": "adaptive"},
                )
                if response.status_code == 422:
                    reason = "no_compatible_stream"
                elif response.status_code >= 500:
                    reason = "backend_unavailable"
                elif response.status_code >= 400:
                    reason = "resolver_error"
                else:
                    normalized = normalize_candidate(
                        response.json(),
                        minimum_quality_height=settings.kids_resolver_min_quality_height,
                    )
                    if normalized is None:
                        reason = "invalid_candidate"
                    else:
                        candidate, quality, codec, resolved_at, expires_at = normalized
                        await db.kids_resolve_success(
                            item_id=job["item_id"],
                            candidate=candidate,
                            quality_height=quality,
                            codec=codec,
                            resolved_at=resolved_at,
                            expires_at=expires_at,
                        )
                        counts["ready"] += 1
                        continue
            except (httpx.HTTPError, ValueError, TypeError):
                reason = "backend_unavailable"
            await db.kids_resolve_failure(item_id=job["item_id"], reason_code=reason)
            counts["retry"] += 1
            counts[reason] = counts.get(reason, 0) + 1
        await db.set_setting("kids_resolver_last_success_at", datetime.now(timezone.utc).isoformat())
        logger.info("kids resolver completed counts=%s", counts)
        return counts
    finally:
        if owns_client:
            await client.aclose()


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings = Settings()
    await run_once(db=Database(settings.db_path), settings=settings)


if __name__ == "__main__":
    asyncio.run(_main())
