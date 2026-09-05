from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..config import Settings
from ..db import Database
from .blocklists import BlocklistService
from .judge import JudgeService
from .kids_classifier import OpenCodexKidsClassifier
from .kids_catalog import (
    KIDS_MIN_RELAY_LIFETIME,
    KIDS_PRACTICAL_CANDIDATE_TTL,
    KIDS_SIGNED_URL_MARGIN,
    _signed_stream_expiry,
    catalog_item_input_hash,
    kids_candidate_usable_until,
)
from .kids_ingest import YouTubeKidsCDP


logger = logging.getLogger(__name__)

PRACTICAL_CANDIDATE_TTL = KIDS_PRACTICAL_CANDIDATE_TTL
PRACTICAL_TTL = PRACTICAL_CANDIDATE_TTL
SIGNED_URL_MARGIN = KIDS_SIGNED_URL_MARGIN
MIN_RELAY_LIFETIME = KIDS_MIN_RELAY_LIFETIME
MIN_CANDIDATE_QUALITY_HEIGHT = 720
MAX_CANDIDATE_QUALITY_HEIGHT = 1080
KIDS_WATCH_URL = "https://www.youtubekids.com/watch?v="


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _dimension(item: dict[str, Any], name: str) -> int:
    try:
        return int(item.get(name) or 0)
    except (TypeError, ValueError):
        return 0


def _bitrate(item: dict[str, Any]) -> int:
    try:
        return int(item.get("abr") or item.get("tbr") or item.get("vbr") or 0)
    except (TypeError, ValueError):
        return 0


def _portrait(item: dict[str, Any]) -> bool:
    width, height = _dimension(item, "width"), _dimension(item, "height")
    return width > 0 and height > 0 and height > width


def _portrait_only(dump: dict[str, Any]) -> bool:
    if not isinstance(dump, dict):
        return False
    if _portrait(dump):
        return True
    formats = dump.get("formats")
    if not isinstance(formats, list):
        return False
    videos = [
        item
        for item in formats
        if isinstance(item, dict)
        and str(item.get("vcodec", "")) not in {"", "none"}
    ]
    return bool(videos) and all(_portrait(item) for item in videos)


def _headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    allowed = {"User-Agent", "Origin", "Referer", "X-Goog-Visitor-Id"}
    return {
        str(name): str(header)
        for name, header in value.items()
        if str(name) in allowed and isinstance(header, str)
    }


def candidate_expiry(
    candidate: dict[str, Any],
    *,
    now: datetime | None = None,
    resolved_at: datetime | None = None,
) -> datetime | None:
    current = now or _now()
    return kids_candidate_usable_until(
        candidate,
        now=current,
        resolved_at=resolved_at or current,
    )


def select_candidate(
    dump: dict[str, Any],
    *,
    minimum_height: int = MIN_CANDIDATE_QUALITY_HEIGHT,
    maximum_height: int = MAX_CANDIDATE_QUALITY_HEIGHT,
) -> dict[str, Any] | None:
    if (
        not isinstance(dump, dict)
        or _portrait(dump)
        or type(minimum_height) is not int
        or type(maximum_height) is not int
        or not MIN_CANDIDATE_QUALITY_HEIGHT
        <= minimum_height
        <= maximum_height
        <= MAX_CANDIDATE_QUALITY_HEIGHT
    ):
        return None
    formats = dump.get("formats")
    if not isinstance(formats, list):
        return None
    now = _now()
    useful_until = now + MIN_RELAY_LIFETIME
    audio = max(
        (
            item
            for item in formats
            if isinstance(item, dict)
            and isinstance(item.get("url"), str)
            and _signed_stream_expiry(item["url"]) is not None
            and _signed_stream_expiry(item["url"]) > useful_until
            and str(item.get("vcodec", "")) == "none"
            and str(item.get("acodec", "")) not in {"", "none"}
        ),
        key=_bitrate,
        default=None,
    )
    videos = [
        item
        for item in formats
        if isinstance(item, dict)
        and isinstance(item.get("url"), str)
        and _signed_stream_expiry(item["url"]) is not None
        and _signed_stream_expiry(item["url"]) > useful_until
        and not _portrait(item)
        and str(item.get("vcodec", "")) not in {"", "none"}
        and str(item.get("acodec", "")) == "none"
        and _dimension(item, "width") > 0
        and _dimension(item, "height") > 0
        and minimum_height <= _dimension(item, "height") <= maximum_height
    ]
    if not audio or not videos:
        return None
    videos.sort(
        key=lambda item: (
            any(codec in str(item.get("vcodec", "")).lower() for codec in ("avc", "h264", "hevc")),
            _dimension(item, "height"),
            _bitrate(item),
        ),
        reverse=True,
    )
    selected = videos[0]
    candidate = {
        "kind": "adaptive_mpv",
        "media_url": str(selected["url"]),
        "audio_url": str(audio["url"]),
        "quality_height": _dimension(selected, "height"),
        "codec": str(selected.get("vcodec") or ""),
        "video_headers": _headers(selected.get("http_headers")),
        "audio_headers": _headers(audio.get("http_headers")),
    }
    return candidate if candidate_expiry(candidate, now=now) else None


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
    required = {"media_url", "audio_url", "quality_height", "codec", "video_headers", "audio_headers"}
    resolved_datetime = datetime.fromisoformat(resolved_at)
    earliest_expiry = candidate_expiry(
        candidate,
        now=_now(),
        resolved_at=resolved_datetime,
    )
    if earliest_expiry is None:
        return None
    if not required.issubset(candidate):
        return None
    return candidate, quality, codec, resolved_at, earliest_expiry.isoformat()


CookieProvider = Callable[[], Awaitable[list[dict[str, Any]]]]


class YtDlpResolver:
    def __init__(
        self,
        *,
        cookie_provider: CookieProvider,
        js_runtime: str = "node",
        timeout_seconds: int = 35,
        minimum_quality_height: int = MIN_CANDIDATE_QUALITY_HEIGHT,
    ):
        self.cookie_provider = cookie_provider
        self.js_runtime = js_runtime
        self.timeout_seconds = timeout_seconds
        self.minimum_quality_height = minimum_quality_height

    async def resolve(self, video_id: str) -> dict[str, Any] | None:
        try:
            async with asyncio.timeout(max(0.001, float(self.timeout_seconds))):
                return await self._resolve_with_fallback(video_id)
        except TimeoutError:
            return None

    async def _resolve_with_fallback(self, video_id: str) -> dict[str, Any] | None:
        try:
            anonymous = await self._extract(video_id)
        except Exception:
            anonymous = None
        candidate = (
            select_candidate(anonymous, minimum_height=self.minimum_quality_height)
            if anonymous
            else None
        )
        if candidate is not None or (anonymous is not None and _portrait_only(anonymous)):
            return candidate
        cookies = await self.cookie_provider()
        if not cookies:
            return None
        try:
            authenticated = await self._extract(video_id, cookies=cookies)
        except Exception:
            return None
        return (
            select_candidate(authenticated, minimum_height=self.minimum_quality_height)
            if authenticated
            else None
        )

    async def _extract(
        self,
        video_id: str,
        *,
        cookies: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        cookie_path: Path | None = None
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--js-runtimes",
            self.js_runtime,
            "--remote-components",
            "ejs:github",
            "--no-playlist",
            "--dump-json",
            "--skip-download",
        ]
        try:
            if cookies:
                cookie_path = _cookie_file(cookies)
                command.extend(["--cookies", str(cookie_path)])
            command.append(f"{KIDS_WATCH_URL}{video_id}")
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _stderr = await process.communicate()
            except (asyncio.CancelledError, TimeoutError):
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
                raise
            if process.returncode:
                raise RuntimeError("yt-dlp failed")
            return _json_output(stdout.decode(errors="replace"))
        finally:
            if cookie_path is not None:
                cookie_path.unlink(missing_ok=True)


def _json_output(value: str) -> dict[str, Any]:
    for line in value.splitlines():
        try:
            result = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result
    raise RuntimeError("yt-dlp returned no JSON")


def _cookie_file(cookies: list[dict[str, Any]]) -> Path:
    handle, raw_path = tempfile.mkstemp(prefix="sentinel-kids-", suffix=".cookies")
    path = Path(raw_path)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write("# Netscape HTTP Cookie File\n")
            for cookie in cookies:
                domain = str(cookie.get("domain", ""))
                name = str(cookie.get("name", ""))
                value = str(cookie.get("value", ""))
                if not domain or not name or not value or "\t" in name + value:
                    continue
                stream.write(
                    "\t".join(
                        (
                            domain,
                            "TRUE" if domain.startswith(".") else "FALSE",
                            str(cookie.get("path") or "/"),
                            "TRUE" if cookie.get("secure") else "FALSE",
                            str(max(0, int(cookie.get("expires") or 0))),
                            name,
                            value.replace("\n", "").replace("\r", ""),
                        )
                    )
                    + "\n"
                )
        path.chmod(0o600)
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


async def run_once(
    *,
    db: Database,
    settings: Settings,
    resolver: YtDlpResolver | None = None,
    classifier: OpenCodexKidsClassifier | None = None,
    judge: JudgeService | None = None,
) -> dict[str, int]:
    await db.init()
    db.kids_item_policy_version = settings.kids_item_policy_version
    db.kids_item_recheck_seconds = settings.kids_item_recheck_seconds
    await db.kids_resolve_sync_backlog(
        minimum_quality_height=settings.kids_resolver_min_quality_height,
    )
    judge = judge or JudgeService(db)
    assessment_candidates = await db.kids_item_assessment_candidates(
        limit=settings.kids_item_assessment_batch_size,
    )
    counts: dict[str, int] = {
        "assessed": 0,
        "assessment_uncertain": 0,
        "assessment_blocklist": 0,
        "assessment_budget_exhausted": 0,
        "assessment_error": 0,
        "infrastructure_error": 0,
        "claimed": 0,
        "ready": 0,
        "retry": 0,
    }
    owned_classifier = False
    if assessment_candidates and classifier is None:
        classifier = OpenCodexKidsClassifier(
            base_url=settings.opencodex_base_url,
            model=settings.opencodex_model,
        )
        owned_classifier = True
    assessment_failed = False
    try:
        if classifier is not None:
            try:
                await classifier.check_model()
                await db.set_setting("kids_resolver_classifier_status", "ready")
            except Exception:
                counts["assessment_error"] += 1
                counts["infrastructure_error"] = 1
                await db.set_setting("kids_resolver_classifier_status", "unavailable")
                assessment_failed = True
        for assessment in assessment_candidates:
            if assessment_failed:
                break
            item = assessment["item"]
            source = assessment["source"]
            if await judge.match_catalog_item_blocklist(item, source):
                counts["assessment_blocklist"] += 1
                continue
            if not await db.kids_item_assessment_budget_take(
                settings.kids_item_assessment_daily_limit,
            ):
                counts["assessment_budget_exhausted"] += 1
                break
            video_metadata = {
                "video_id": str(item.get("video_id") or ""),
                "title": str(item.get("title") or ""),
                "channel_id": str(item.get("channel_id") or ""),
                "channel_title": str(item.get("channel_title") or ""),
                "thumbnail_url": str(item.get("thumbnail_url") or ""),
            }
            metadata = {
                "kind": "video",
                **video_metadata,
                "source_id": item.get("source_id"),
                "source_reference": str(source.get("reference") or ""),
                "source_title": str(source.get("title") or ""),
                "sample_videos": [video_metadata],
            }
            try:
                decision = await classifier.classify(metadata)
                await db.catalog_item_safety_update(
                    int(item["id"]),
                    verdict=decision["verdict"],
                    language=decision["language"],
                    content_kind=decision["content_kind"],
                    age_suitability=decision["age_suitability"],
                    reason=str(decision.get("reason") or "Item classification"),
                    actor="kids-item-classifier",
                    correlation_id=f"kids-item-safety-{item['id']}",
                    policy_version=settings.kids_item_policy_version,
                    input_hash=catalog_item_input_hash(item),
                    sync_backlog=False,
                )
                counts["assessed"] += 1
                if decision["verdict"] == "UNCERTAIN":
                    counts["assessment_uncertain"] += 1
                await db.set_setting("kids_resolver_classifier_status", "ready")
            except Exception:
                counts["assessment_error"] += 1
                counts["infrastructure_error"] = 1
                await db.set_setting("kids_resolver_classifier_status", "unavailable")
                assessment_failed = True
                break
        await db.kids_resolve_sync_backlog(
            minimum_quality_height=settings.kids_resolver_min_quality_height,
        )
    finally:
        if owned_classifier and classifier is not None:
            await classifier.close()
    if assessment_failed:
        logger.info("kids resolver stopped after assessment infrastructure error counts=%s", counts)
        return counts
    jobs = await db.kids_resolve_claim_due(
        limit=settings.kids_resolver_batch_size,
        refresh_margin_seconds=settings.kids_resolve_refresh_margin_seconds,
    )
    counts["claimed"] = len(jobs)
    if resolver is None:
        browser = YouTubeKidsCDP(settings.kids_resolver_cdp_url)
        resolver = YtDlpResolver(
            cookie_provider=browser.youtube_cookies,
            js_runtime=settings.kids_resolver_js_runtime,
            timeout_seconds=settings.kids_resolver_timeout_seconds,
            minimum_quality_height=settings.kids_resolver_min_quality_height,
        )
    for job in jobs:
        reason = "no_compatible_stream"
        try:
            candidate = await resolver.resolve(str(job["video_id"]))
            resolved_at = _now()
            expires_at = candidate_expiry(candidate, now=resolved_at) if candidate else None
            quality_height = candidate.get("quality_height") if isinstance(candidate, dict) else None
            if (
                candidate is None
                or expires_at is None
                or type(quality_height) is not int
                or not settings.kids_resolver_min_quality_height <= quality_height <= MAX_CANDIDATE_QUALITY_HEIGHT
                or candidate.get("quality_height") != quality_height
            ):
                reason = "no_compatible_stream"
            else:
                await db.kids_resolve_success(
                    item_id=job["item_id"],
                    candidate=candidate,
                    quality_height=quality_height,
                    codec=str(candidate["codec"]),
                    resolved_at=_iso(resolved_at),
                    expires_at=_iso(expires_at),
                    minimum_quality_height=settings.kids_resolver_min_quality_height,
                )
                counts["ready"] += 1
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            reason = "resolver_error"
        await db.kids_resolve_failure(item_id=job["item_id"], reason_code=reason)
        counts["retry"] += 1
        counts[reason] = counts.get(reason, 0) + 1
    await db.set_setting("kids_resolver_last_success_at", _iso(_now()))
    logger.info("kids resolver completed counts=%s", counts)
    return counts


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("yt_dlp").setLevel(logging.WARNING)
    settings = Settings()
    db = Database(
        settings.db_path,
        kids_item_policy_version=settings.kids_item_policy_version,
        kids_item_recheck_seconds=settings.kids_item_recheck_seconds,
    )
    await db.init()
    blocklists = BlocklistService(settings)
    await blocklists.reload(db)
    judge = JudgeService(db, blocklists=blocklists)
    await judge.reconcile_catalog_policy()
    classifier = OpenCodexKidsClassifier(
        base_url=settings.opencodex_base_url,
        model=settings.opencodex_model,
    )
    try:
        await run_once(
            db=db,
            settings=settings,
            classifier=classifier,
            judge=judge,
        )
    finally:
        await classifier.close()


if __name__ == "__main__":
    asyncio.run(_main())
