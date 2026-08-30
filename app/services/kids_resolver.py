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


def normalize_candidate(payload: Any) -> tuple[dict[str, Any], int, str, str, str] | None:
    if not isinstance(payload, dict) or payload.get("status") != "ready":
        return None
    candidate = payload.get("candidate")
    expires_at = _utc_timestamp(payload.get("expires_at"))
    resolved_at = _utc_timestamp(payload.get("resolved_at"))
    if not isinstance(candidate, dict) or not expires_at or not resolved_at:
        return None
    quality = candidate.get("quality_height")
    codec = candidate.get("codec")
    if not isinstance(quality, int) or not 144 <= quality <= 1080 or not isinstance(codec, str) or not codec:
        return None
    media_expiry = _signed_expiry(candidate.get("media_url"))
    audio_expiry = _signed_expiry(candidate.get("audio_url"))
    if media_expiry is None or audio_expiry is None:
        return None
    earliest_expiry = min(media_expiry, audio_expiry)
    if earliest_expiry <= datetime.now(timezone.utc) + timedelta(seconds=120):
        return None
    required = {"media_url", "audio_url", "quality_height", "codec", "video_headers", "audio_headers"}
    if not required.issubset(candidate) or not isinstance(candidate["video_headers"], dict) or not isinstance(candidate["audio_headers"], dict):
        return None
    return candidate, quality, codec, resolved_at, earliest_expiry.isoformat()


async def run_once(
    *,
    db: Database,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> dict[str, int]:
    await db.init()
    await db.kids_resolve_sync_backlog()
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
                    params={"target_height": 1080},
                )
                if response.status_code == 422:
                    reason = "no_compatible_stream"
                elif response.status_code >= 500:
                    reason = "backend_unavailable"
                elif response.status_code >= 400:
                    reason = "resolver_error"
                else:
                    normalized = normalize_candidate(response.json())
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
    settings = Settings()
    await run_once(db=Database(settings.db_path), settings=settings)


if __name__ == "__main__":
    asyncio.run(_main())
