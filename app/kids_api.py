from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from itertools import chain, islice, zip_longest
from pathlib import Path
from typing import Any, AsyncGenerator
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from .models import (
    CatalogItemRequest,
    CatalogSourceRequest,
    CatalogTransitionRequest,
    KidsDataplaneEventRequest,
    KidsKillSwitchRequest,
    KidsPlaybackSessionRequest,
    KidsSourceClassificationRequest,
    KidsSourcePosterRequest,
    KidsSourceProfilesRequest,
    KidsWatchEventRequest,
)
from .services.kids_database import (
    KIDS_CHANNEL_ART_HOSTS,
    KIDS_VIDEO_THUMBNAIL_HOSTS,
    _kids_channel_avatar_is_proxyable,
)
from .services.kids_catalog import _parse_utc


KIDS_DATAPLANE_POLICY_VERSION = "sentinel-kids-v1"
KIDS_PROFILE_AVATAR_MAX_BYTES = 10 * 1024 * 1024
KIDS_CHANNEL_ART_MAX_BYTES = 8 * 1024 * 1024
KIDS_SHELF_ICONS = {
    "new": "sparkles",
    "learning-nl": "book.fill",
    "fun-en": "globe",
    "fun-nl": "star.fill",
    "again": "arrow.counterclockwise",
}
KIDS_PROFILE_SHELF_IDS = {
    "noah": ("new", "learning-nl", "fun-en", "fun-nl", "again"),
    "felix": ("new", "learning-nl", "fun-nl", "again"),
}
KIDS_SHELF_PAGE_SIZE = 12
KIDS_SHELF_TARGET = 72
KIDS_SHELF_MIN_AGAIN = 3
KIDS_SHELF_COOLDOWN = timedelta(days=7)
router = APIRouter()


@router.get("/api/kids/catalog/revision")
async def api_catalog_revision(request: Request) -> dict[str, Any]:
    return {"revision": await request.app.state.runtime.db.catalog_revision()}


def _encode_kids_cursor(session_id: str, offset: int, profile: str) -> str:
    payload = json.dumps(
        [session_id, int(offset), profile],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_kids_cursor(value: str) -> tuple[str, int, str]:
    try:
        if not value or len(value) > 512 or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in value
        ):
            raise ValueError
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(decoded)
        if (
            not isinstance(payload, list)
            or len(payload) != 3
            or not isinstance(payload[0], str)
            or not payload[0]
            or len(payload[0]) > 128
            or type(payload[1]) is not int
            or payload[1] < 0
            or not isinstance(payload[2], str)
            or not payload[2]
            or len(payload[2]) > 64
        ):
            raise ValueError
        return payload[0], payload[1], payload[2]
    except (ValueError, TypeError, json.JSONDecodeError):
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_cursor", "message": "Invalid Kids feed cursor."},
        ) from None


def _kids_unavailable(state: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={"code": "kids_unavailable", "state": state},
    )


def _kids_source_poster_response(result: dict[str, Any]) -> dict[str, Any]:
    source = result["source"]
    effective_poster = result["effective_poster"]
    item = effective_poster.get("item")
    return {
        "source": {
            "id": int(source["id"]),
            "kind": str(source.get("kind") or ""),
            "reference": str(source.get("reference") or ""),
            "title": str(source.get("title") or ""),
            "state": str(source.get("state") or ""),
            "safety_verdict": str(source.get("safety_verdict") or ""),
            "genuine_avatar_url": str(source.get("genuine_avatar_url") or ""),
            "poster_item_id": (
                int(source["poster_item_id"])
                if source.get("poster_item_id") is not None
                else None
            ),
            "effective_poster_thumbnail_url": str(
                source.get("effective_poster_thumbnail_url") or ""
            ),
            "revision": int(source.get("revision") or 0),
        },
        "effective_poster": {
            "mode": str(effective_poster.get("mode") or "automatic"),
            "item": (
                {
                    "id": int(item["id"]),
                    "title": str(item.get("title") or ""),
                    "thumbnail_url": str(item.get("thumbnail_url") or ""),
                    "state": str(item.get("state") or ""),
                }
                if item is not None
                else None
            ),
        },
    }


def _kids_lease_failure(result: dict[str, Any]) -> HTTPException:
    failure = result.get("status", "ineligible")
    if failure == "not_found":
        status_code = 404
    elif failure in {"expired", "policy_mismatch"}:
        status_code = 409
    else:
        status_code = 403
    return HTTPException(
        status_code=status_code,
        detail={"code": f"kids_{failure}", "message": "Kids playback is not authorized."},
    )


async def _kids_profile(request: Request, profile: str, *, require_enabled: bool = True) -> dict[str, Any]:
    value = str(profile or "").strip().lower()
    row = await request.app.state.runtime.db.kids_profile_get(value)
    if row is None or (require_enabled and not row["enabled"]):
        raise HTTPException(
            status_code=404,
            detail={"code": "kids_profile_not_found", "message": "Kids profile is unavailable."},
        )
    return row


def _kids_channel_opaque_id(channel: dict[str, Any]) -> str:
    identity = f"{channel.get('kind', '')}\x00{channel.get('reference', '')}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _kids_shelf_language_rank(shelf: str, language: str) -> int | None:
    if shelf in {"again", "new"}:
        return 0
    if shelf in {"learning-nl", "fun-nl"}:
        return {"nl": 0, "mixed": 1}.get(language)
    if shelf == "fun-en":
        return {"en": 0, "mixed": 1}.get(language)
    return None


def _kids_shelf_daily_key(day: str, profile: str, shelf: str, item_id: int) -> str:
    return hashlib.sha256(
        f"{day}\x00{profile}\x00{shelf}\x00{item_id}".encode("utf-8")
    ).hexdigest()


def _kids_shelf_candidate_allowed(
    item: dict[str, Any],
    *,
    profile: str,
    shelf: str,
    cooldown_after: datetime,
) -> bool:
    if shelf == "again":
        return _parse_utc(item.get("_profile_history_at")) is not None
    if shelf == "new":
        language = str(item.get("_source_language") or "unknown").strip().lower()
        return (
            _parse_utc(item.get("_profile_history_at")) is None
            and language
            in ({"nl", "mixed"} if profile == "felix" else {"nl", "en", "mixed"})
        )
    raw_completed_at = item.get("_profile_completed_at")
    completed_at = _parse_utc(raw_completed_at)
    if str(raw_completed_at or "").strip() and (
        completed_at is None or completed_at > cooldown_after
    ):
        return False
    content_kind = str(item.get("_source_content_kind") or "unknown").strip().lower()
    if shelf == "learning-nl" and content_kind not in {"learning", "mixed"}:
        return False
    if shelf in {"fun-en", "fun-nl"} and content_kind not in {
        "entertainment",
        "mixed",
    }:
        return False
    return (
        _kids_shelf_language_rank(
            shelf,
            str(item.get("_source_language") or "unknown").strip().lower(),
        )
        is not None
    )


def _kids_shelf_order_key(
    item: dict[str, Any],
    *,
    day: str,
    profile: str,
    shelf: str,
) -> tuple[Any, ...]:
    item_id = int(item["id"])
    if shelf == "new":
        return (-item_id,)
    if shelf == "again":
        history_at = _parse_utc(item.get("_profile_history_at"))
        return (
            -(history_at.timestamp() if history_at is not None else 0),
            _kids_shelf_daily_key(day, profile, shelf, item_id),
            item_id,
        )
    content_kind = str(item.get("_source_content_kind") or "unknown").strip().lower()
    language = str(item.get("_source_language") or "unknown").strip().lower()
    language_rank = _kids_shelf_language_rank(shelf, language)
    content_rank = 0 if content_kind == (
        "learning" if shelf == "learning-nl" else "entertainment"
    ) else 1
    return (
        language_rank if language_rank is not None else 2,
        content_rank,
        _kids_shelf_daily_key(day, profile, shelf, item_id),
        item_id,
    )


def _kids_shelf_round_robin(
    items: list[dict[str, Any]],
    *,
    profile: str,
    shelf: str,
    day: str,
    limit: int,
) -> list[dict[str, Any]]:
    by_source: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        try:
            item_id = int(item["id"])
            source_id = int(item["source_id"])
        except (KeyError, TypeError, ValueError):
            continue
        by_source.setdefault(source_id, []).append(item)
    for source_items in by_source.values():
        source_items.sort(
            key=lambda item: _kids_shelf_order_key(
                item,
                day=day,
                profile=profile,
                shelf=shelf,
            )
        )
    source_order = sorted(
        by_source,
        key=lambda source_id: _kids_shelf_order_key(
            by_source[source_id][0],
            day=day,
            profile=profile,
            shelf=shelf,
        ),
    )
    return list(
        islice(
            (
                item
                for item in chain.from_iterable(
                    zip_longest(*(by_source[source_id] for source_id in source_order))
                )
                if item is not None
            ),
            max(0, limit),
        )
    )


def _select_kids_shelves(
    items: list[dict[str, Any]],
    *,
    profile: str,
    day: str,
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    cooldown_after = now - KIDS_SHELF_COOLDOWN
    shelf_ids = KIDS_PROFILE_SHELF_IDS.get(
        str(profile or "").strip().lower(),
        KIDS_PROFILE_SHELF_IDS["noah"],
    )
    selected: dict[str, list[dict[str, Any]]] = {
        shelf: [] for shelf in shelf_ids if shelf != "again"
    }
    selected_ids: set[int] = set()
    shelf_limit = KIDS_SHELF_TARGET
    candidates_by_shelf = {
        shelf: [
            item
            for item in items
            if _kids_shelf_candidate_allowed(
                item,
                profile=profile,
                shelf=shelf,
                cooldown_after=cooldown_after,
            )
        ]
        for shelf in shelf_ids
    }
    if len(candidates_by_shelf.get("again", [])) < KIDS_SHELF_MIN_AGAIN:
        candidates_by_shelf.pop("again", None)
    if "again" in candidates_by_shelf:
        selected["again"] = _kids_shelf_round_robin(
            candidates_by_shelf["again"],
            profile=profile,
            shelf="again",
            day=day,
            limit=shelf_limit,
        )
        selected_ids.update(int(item["id"]) for item in selected["again"])
    priority = [
        shelf
        for shelf in shelf_ids
        if shelf not in {"again", "new"} and shelf in candidates_by_shelf
    ]
    if "new" in candidates_by_shelf:
        priority.append("new")

    ranked = {
        shelf: _kids_shelf_round_robin(
            candidates_by_shelf[shelf],
            profile=profile,
            shelf=shelf,
            day=day,
            limit=len(candidates_by_shelf[shelf]),
        )
        for shelf in priority
    }
    positions = {shelf: 0 for shelf in priority}
    while any(len(selected.get(shelf, [])) < shelf_limit for shelf in priority):
        added = False
        for shelf in priority:
            if len(selected.get(shelf, [])) >= shelf_limit:
                continue
            while positions[shelf] < len(ranked[shelf]):
                item = ranked[shelf][positions[shelf]]
                positions[shelf] += 1
                item_id = int(item["id"])
                if item_id in selected_ids:
                    continue
                selected.setdefault(shelf, []).append(item)
                selected_ids.add(item_id)
                added = True
                break
        if not added:
            break
    return selected


async def _kids_shelf_context(runtime: Any) -> tuple[str, datetime]:
    settings = await runtime.db.all_settings()
    timezone_name = str(settings.get("timezone") or "UTC")
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
    local_now = datetime.now(timezone.utc).astimezone(zone)
    local_midnight = datetime.combine(
        local_now.date(),
        datetime.min.time(),
        tzinfo=zone,
    )
    return local_now.date().isoformat(), local_midnight.astimezone(timezone.utc)


def _kids_channel_accessibility_label(channel: dict[str, Any]) -> str:
    label = str(channel.get("title") or "").strip()
    reference = str(channel.get("reference") or "").strip()
    lowered = label.casefold()
    if (
        not label
        or label == reference
        or "http://" in lowered
        or "https://" in lowered
        or "youtube.com/" in lowered
        or "youtu.be/" in lowered
    ):
        return "Kids channel"
    return label[:200]


def _kids_channel_artwork_url(
    request: Request,
    opaque_id: str,
    artwork: str,
    profile: str,
    revision: int,
) -> str:
    base_url = str(request.base_url).rstrip("/")
    return (
        f"{base_url}/v1/kids/channels/{opaque_id}/artwork/{artwork}"
        f"?profile={quote(profile, safe='')}&v={revision}"
    )


async def _kids_current_channel(
    request: Request,
    profile: str,
    opaque_id: str,
) -> tuple[str, dict[str, Any]]:
    runtime: Any = request.app.state.runtime
    profile_row = await _kids_profile(request, profile)
    await runtime.reconcile_kids_catalog_policy()
    if await runtime.kids_policy_state() != "ready":
        raise _kids_unavailable(await runtime.kids_policy_state())
    channels = await runtime.db.kids_eligible_channels(
        profile=profile_row["slug"],
        minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
        minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
    )
    for channel in channels:
        if _kids_channel_opaque_id(channel) == opaque_id:
            return profile_row["slug"], channel
    raise HTTPException(
        status_code=404,
        detail={"code": "kids_channel_not_found", "message": "Kids channel is unavailable."},
    )


async def _kids_proxy_image(
    runtime: Any,
    image_url: str,
    *,
    allowed_hosts: frozenset[str],
    unavailable_detail: str = "Kids artwork is unavailable",
    upstream_detail: str = "Kids artwork upstream unavailable",
) -> Response:
    try:
        parsed = urlsplit(image_url)
        port = parsed.port
    except ValueError:
        port = None
        parsed = None
    if (
        parsed is None
        or parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
    ):
        raise HTTPException(status_code=404, detail=unavailable_detail)
    try:
        upstream = await runtime.kids_http_client.get(
            image_url,
            follow_redirects=False,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=upstream_detail) from exc
    content_type = upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        content_length = int(upstream.headers.get("content-length", "0"))
    except ValueError:
        content_length = 0
    if (
        upstream.status_code != 200
        or not content_type.startswith("image/")
        or content_length > KIDS_CHANNEL_ART_MAX_BYTES
        or len(upstream.content) > KIDS_CHANNEL_ART_MAX_BYTES
    ):
        await upstream.aclose()
        raise HTTPException(status_code=502, detail=upstream_detail)
    content = upstream.content
    await upstream.aclose()
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _kids_profile_avatar_path(request: Request, profile: str) -> Path:
    return Path(request.app.state.runtime.settings.data_dir) / "profile-avatars" / profile


def _kids_profile_avatar_media_type(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in {b"avif", b"avis"}:
            return "image/avif"
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
    return None


async def _kids_profile_avatar_body(request: Request) -> bytes:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > KIDS_PROFILE_AVATAR_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Profile image exceeds 10 MB")
    return bytes(data)


def kids_profile_payload(request: Request, profile: dict[str, Any]) -> dict[str, Any]:
    avatar_path = _kids_profile_avatar_path(request, profile["slug"])
    avatar_url = None
    if avatar_path.is_file():
        avatar_url = (
            f"{str(request.base_url).rstrip('/')}/api/kids/profiles/"
            f"{profile['slug']}/avatar?v={avatar_path.stat().st_mtime_ns}"
        )
    return {**profile, "avatar_url": avatar_url}


async def _kids_checked_lease(
    request: Request,
    lease_id: str,
    *,
    reconcile: bool = True,
) -> tuple[Any, dict[str, Any]]:
    runtime: Any = request.app.state.runtime
    if reconcile:
        await runtime.reconcile_kids_catalog_policy()
    state = await runtime.kids_policy_state()
    if state != "ready":
        raise _kids_unavailable(state)
    lease = await runtime.db.kids_relay_lease_get(
        lease_id,
        minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
    )
    if lease is None:
        raise HTTPException(
            status_code=403,
            detail={"code": "kids_lease_inactive", "message": "Kids playback lease is inactive."},
        )
    return runtime, lease


def _kids_candidate_headers(
    candidate: dict[str, Any],
    stream_name: str,
    request: Request,
) -> dict[str, str]:
    headers: dict[str, str] = {"Accept-Encoding": "identity"}
    allowed = {"user-agent", "origin", "referer", "x-goog-visitor-id"}
    raw_headers = candidate.get(f"{stream_name}_headers", {})
    if isinstance(raw_headers, dict):
        for name, value in raw_headers.items():
            if str(name).lower() in allowed and isinstance(value, str):
                headers[str(name)] = value
    range_header = request.headers.get("range")
    if range_header:
        headers["Range"] = range_header
    elif request.method == "GET":
        headers["Range"] = "bytes=0-"
    return headers


def _kids_upstream_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: response.headers[name]
        for name in ("accept-ranges", "content-length", "content-range", "content-type")
        if name in response.headers
    }


@router.get("/v1/kids/channels")
async def kids_channels(
    request: Request,
    profile: str = Query(default="noah", min_length=1, max_length=64),
) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    profile_row = await _kids_profile(request, profile)
    await runtime.reconcile_kids_catalog_policy()
    state = await runtime.kids_policy_state()
    revision = await runtime.db.catalog_revision()
    if state != "ready":
        return {
            "state": state,
            "catalog_revision": str(revision),
            "retry_after_seconds": 30,
            "channels": [],
        }
    channels = await runtime.db.kids_eligible_channels(
        profile=profile_row["slug"],
        minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
        minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
    )
    response_channels = []
    for channel in channels:
        opaque_id = _kids_channel_opaque_id(channel)
        poster_url = None
        if channel.get("poster_item"):
            poster_url = _kids_channel_artwork_url(
                request,
                opaque_id,
                "background",
                profile_row["slug"],
                revision,
            )
        avatar_url = (
            _kids_channel_artwork_url(
                request,
                opaque_id,
                "avatar",
                profile_row["slug"],
                revision,
            )
            if _kids_channel_avatar_is_proxyable(channel.get("avatar_url"))
            else None
        )
        response_channels.append(
            {
                "id": opaque_id,
                "poster_background_url": poster_url,
                "avatar_url": avatar_url,
                "accessibility_label": _kids_channel_accessibility_label(channel),
            }
        )
    return {
        "state": state,
        "catalog_revision": str(revision),
        "retry_after_seconds": 30,
        "channels": response_channels,
    }


@router.get("/v1/kids/shelves")
async def kids_shelves(
    request: Request,
    profile: str = Query(default="noah", min_length=1, max_length=64),
) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    profile_row = await _kids_profile(request, profile)
    shelf_ids = KIDS_PROFILE_SHELF_IDS[profile_row["slug"]]
    await runtime.reconcile_kids_catalog_policy()
    state = await runtime.kids_policy_state()
    revision = await runtime.db.catalog_revision()
    empty_shelves = [
        {
            "id": shelf,
            "icon": KIDS_SHELF_ICONS[shelf],
            "items": [],
            "next_cursor": None,
        }
        for shelf in shelf_ids
        if shelf != "again"
    ]
    if state != "ready":
        return {
            "state": state,
            "catalog_revision": str(revision),
            "retry_after_seconds": 30,
            "shelves": empty_shelves,
        }

    candidates = await runtime.db.kids_eligible_feed_list(
        runtime.settings.kids_playback_min_remaining_seconds,
        runtime.settings.kids_resolver_min_quality_height,
        profile=profile_row["slug"],
        include_shelf_metadata=True,
    )
    day, day_boundary = await _kids_shelf_context(runtime)
    selected = _select_kids_shelves(
        candidates,
        profile=profile_row["slug"],
        day=day,
        now=day_boundary,
    )
    active_shelf_ids = tuple(shelf for shelf in shelf_ids if shelf in selected)
    daily_slots = await runtime.db.kids_daily_library_get_or_create(
        day=day,
        profile=profile_row["slug"],
        shelf_limit=KIDS_SHELF_TARGET,
        proposed_item_ids={
            shelf: [int(item["id"]) for item in selected[shelf]]
            for shelf in active_shelf_ids
        },
    )
    candidates_by_id = {int(item["id"]): item for item in candidates}
    cooldown_after = day_boundary - KIDS_SHELF_COOLDOWN
    shelf_items_by_name = {
        shelf: [
            item
            for item_id in daily_slots[shelf]
            if item_id is not None
            and (item := candidates_by_id.get(int(item_id))) is not None
            and _kids_shelf_candidate_allowed(
                item,
                profile=profile_row["slug"],
                shelf=shelf,
                cooldown_after=cooldown_after,
            )
        ]
        for shelf in active_shelf_ids
    }
    response_shelves: list[dict[str, Any]] = []
    base_url = str(request.base_url).rstrip("/")
    for shelf in active_shelf_ids:
        shelf_items = shelf_items_by_name[shelf]
        ordered_item_ids = [int(item["id"]) for item in shelf_items]
        page: dict[str, Any] = {"items": [], "next_offset": None}
        session_id: str | None = None
        if ordered_item_ids:
            session = await runtime.db.kids_feed_session_create(
                profile=profile_row["slug"],
                policy_version=KIDS_DATAPLANE_POLICY_VERSION,
                minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
                minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
                ordered_item_ids=ordered_item_ids,
            )
            session_id = session["id"]
            page = await runtime.db.kids_feed_session_page(
                session_id,
                profile=profile_row["slug"],
                offset=0,
                limit=KIDS_SHELF_PAGE_SIZE,
                policy_version=KIDS_DATAPLANE_POLICY_VERSION,
                minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
                minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
            )
            if page["status"] != "ok":
                page = {"items": [], "next_offset": None}
        response_items = [
            {
                "id": item["asset_id"],
                "thumbnail_url": f"{base_url}/v1/kids/thumbnails/{item['asset_id']}",
                "duration_seconds": item["duration_seconds"],
                "visual_category": item["visual_category"],
            }
            for item in page["items"]
        ]
        response_shelves.append(
            {
                "id": shelf,
                "icon": KIDS_SHELF_ICONS[shelf],
                "items": response_items,
                "next_cursor": (
                    _encode_kids_cursor(
                        session_id,
                        page["next_offset"],
                        profile_row["slug"],
                    )
                    if session_id is not None and page["next_offset"] is not None
                    else None
                ),
            }
        )
    return {
        "state": state,
        "catalog_revision": str(await runtime.db.catalog_revision()),
        "retry_after_seconds": 30,
        "shelves": response_shelves,
    }


@router.get("/v1/kids/channels/{opaque_id}/artwork/background")
async def kids_channel_background(
    opaque_id: str,
    request: Request,
    profile: str = Query(default="noah", min_length=1, max_length=64),
) -> Response:
    _profile, channel = await _kids_current_channel(request, profile, opaque_id)
    poster = channel.get("poster_item")
    if not poster:
        raise HTTPException(status_code=404, detail="Kids channel artwork is unavailable")
    return await _kids_proxy_image(
        request.app.state.runtime,
        str(poster["thumbnail_url"]),
        allowed_hosts=KIDS_VIDEO_THUMBNAIL_HOSTS,
    )


@router.get("/v1/kids/channels/{opaque_id}/artwork/avatar")
async def kids_channel_avatar(
    opaque_id: str,
    request: Request,
    profile: str = Query(default="noah", min_length=1, max_length=64),
) -> Response:
    _profile, channel = await _kids_current_channel(request, profile, opaque_id)
    avatar_url = str(channel.get("avatar_url") or "").strip()
    if not _kids_channel_avatar_is_proxyable(avatar_url):
        raise HTTPException(status_code=404, detail="Kids channel avatar is unavailable")
    return await _kids_proxy_image(
        request.app.state.runtime,
        avatar_url,
        allowed_hosts=KIDS_CHANNEL_ART_HOSTS,
    )


@router.get("/v1/kids/feed")
async def kids_feed(
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=36, ge=1, le=60),
    profile: str | None = Query(default=None, min_length=1, max_length=64),
    channel: str | None = Query(default=None, min_length=1, max_length=128),
) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    requested_profile = str(profile or "").strip().lower()
    requested_channel = str(channel or "").strip()
    await runtime.reconcile_kids_catalog_policy()
    state = await runtime.kids_policy_state()
    if cursor:
        session_id, offset, cursor_profile = _decode_kids_cursor(cursor)
        if requested_profile and requested_profile != cursor_profile:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "kids_feed_profile_mismatch",
                    "message": "Kids feed cursor is no longer valid.",
                },
            )
        profile = (await _kids_profile(request, cursor_profile))["slug"]
    else:
        profile = (await _kids_profile(request, requested_profile or "noah"))["slug"]
    channel_source_id: int | None = None
    if requested_channel:
        _channel_profile, channel_row = await _kids_current_channel(
            request,
            profile,
            requested_channel,
        )
        channel_source_id = int(channel_row["source_id"])
        if cursor:
            binding = await runtime.db.kids_feed_session_binding(session_id)
            if binding is not None and binding["source_id"] != channel_source_id:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "kids_feed_channel_mismatch",
                        "message": "Kids feed cursor is no longer valid.",
                    },
                )
    if not cursor:
        session = await runtime.db.kids_feed_session_create(
            profile=profile,
            policy_version=KIDS_DATAPLANE_POLICY_VERSION,
            minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
            minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
            include_items=state == "ready",
            source_id=channel_source_id,
        )
        session_id, offset = session["id"], 0
    page = await runtime.db.kids_feed_session_page(
        session_id,
        profile=profile,
        offset=offset,
        limit=limit,
        policy_version=KIDS_DATAPLANE_POLICY_VERSION,
        minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
        minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
    )
    if page["status"] != "ok":
        raise HTTPException(
            status_code=409,
            detail={"code": f"kids_feed_{page['status']}", "message": "Kids feed cursor is no longer valid."},
        )
    items = page["items"] if state == "ready" else []
    base_url = str(request.base_url).rstrip("/")
    response_items = [
        {
            "id": item["asset_id"],
            "thumbnail_url": f"{base_url}/v1/kids/thumbnails/{item['asset_id']}",
            "duration_seconds": item["duration_seconds"],
            "visual_category": item["visual_category"],
        }
        for item in items
    ]
    next_offset = page["next_offset"] if state == "ready" else None
    return {
        "state": state,
        "catalog_revision": str(await runtime.db.catalog_revision()),
        "items": response_items,
        "next_cursor": (
            _encode_kids_cursor(session_id, next_offset, profile)
            if next_offset is not None
            else None
        ),
        "retry_after_seconds": 30,
    }


@router.get("/v1/kids/thumbnails/{asset_id}")
async def kids_thumbnail(asset_id: str, request: Request) -> Response:
    runtime: Any = request.app.state.runtime
    await runtime.reconcile_kids_catalog_policy()
    state = await runtime.kids_policy_state()
    if state != "ready":
        raise _kids_unavailable(state)
    asset = await runtime.db.kids_feed_asset(
        asset_id,
        minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
        minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
    )
    if asset is None:
        raise HTTPException(status_code=404, detail="Kids asset not found")
    return await _kids_proxy_image(
        runtime,
        str(asset["thumbnail_url"]),
        allowed_hosts=KIDS_VIDEO_THUMBNAIL_HOSTS,
        unavailable_detail="Kids thumbnail is unavailable",
        upstream_detail="Kids thumbnail upstream unavailable",
    )


@router.post("/v1/kids/playback-sessions")
async def kids_playback_session(
    payload: KidsPlaybackSessionRequest,
    request: Request,
) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    await runtime.reconcile_kids_catalog_policy()
    state = await runtime.kids_policy_state()
    if state != "ready":
        raise _kids_unavailable(state)
    result = await runtime.db.kids_relay_lease_create(
        asset_id=payload.asset_id,
        policy_version=KIDS_DATAPLANE_POLICY_VERSION,
        minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
        minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
    )
    if result["status"] != "ok":
        raise _kids_lease_failure(result)
    base_url = str(request.base_url).rstrip("/")
    lease_id = result["id"]
    return {
        "id": lease_id,
        "session_id": result["session_id"],
        "manifest_url": f"{base_url}/v1/kids/playback-sessions/{lease_id}/manifest",
        "quality_height": result["quality_height"],
        "codec": result["codec"],
        "expires_at": result["expires_at"],
    }


@router.get("/v1/kids/playback-sessions/{lease_id}/manifest")
async def kids_playback_manifest(lease_id: str, request: Request) -> dict[str, Any]:
    runtime, lease = await _kids_checked_lease(request, lease_id)
    base_url = str(request.base_url).rstrip("/")
    prefix = f"{base_url}/v1/kids/playback-sessions/{lease_id}"
    candidate = lease["candidate"]
    return {
        "session_id": lease_id,
        "transport": "adaptive_mpv",
        "video_url": f"{prefix}/video",
        "audio_url": f"{prefix}/audio",
        "status_url": f"{prefix}/status",
        "quality_height": lease["quality_height"],
        "codec": str(candidate.get("codec") or ""),
        "expires_at": lease["expires_at"],
    }


@router.get("/v1/kids/playback-sessions/{lease_id}/status")
async def kids_playback_status(lease_id: str, request: Request) -> Response:
    await _kids_checked_lease(request, lease_id)
    return Response(status_code=204)


@router.api_route(
    "/v1/kids/playback-sessions/{lease_id}/{stream_name}",
    methods=["GET", "HEAD"],
    response_model=None,
)
async def kids_playback_stream(
    lease_id: str,
    stream_name: str,
    request: Request,
) -> Response | StreamingResponse:
    if stream_name not in {"video", "audio"}:
        raise HTTPException(status_code=404, detail="Kids media stream not found")
    runtime, lease = await _kids_checked_lease(request, lease_id, reconcile=False)
    candidate = lease["candidate"]
    source_url = candidate.get("media_url" if stream_name == "video" else "audio_url")
    parsed = urlsplit(str(source_url))
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(status_code=403, detail="Kids media source is unavailable")
    headers = _kids_candidate_headers(candidate, stream_name, request)
    try:
        upstream = await runtime.kids_http_client.send(
            runtime.kids_http_client.build_request(
                request.method,
                str(source_url),
                headers=headers,
            ),
            stream=request.method != "HEAD",
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Kids media upstream unavailable") from exc
    response_headers = _kids_upstream_headers(upstream)
    if request.method == "HEAD" or upstream.status_code >= 400:
        status_code = upstream.status_code
        await upstream.aclose()
        return Response(status_code=status_code, headers=response_headers)

    async def body() -> AsyncGenerator[bytes, None]:
        next_authorization_check = time.monotonic() + 1
        try:
            async for chunk in upstream.aiter_bytes():
                now = time.monotonic()
                if now >= next_authorization_check:
                    if not await _kids_checked_stream(
                        runtime,
                        lease_id,
                        minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
                    ):
                        break
                    next_authorization_check = now + 1
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


async def _kids_checked_stream(
    runtime: Any,
    lease_id: str,
    *,
    minimum_quality_height: int = 720,
) -> bool:
    if await runtime.kids_policy_state() != "ready":
        return False
    return (
        await runtime.db.kids_relay_lease_get(
            lease_id,
            minimum_quality_height=minimum_quality_height,
        )
        is not None
    )


@router.delete("/v1/kids/playback-sessions/{lease_id}")
async def kids_playback_delete(lease_id: str, request: Request) -> dict[str, Any]:
    closed = await request.app.state.runtime.db.kids_relay_lease_close(lease_id)
    return {"closed": closed}


@router.post("/v1/kids/events", status_code=202)
async def kids_dataplane_event(
    payload: KidsDataplaneEventRequest,
    request: Request,
) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    profile = (await _kids_profile(request, payload.profile))["slug"]
    asset = await runtime.db.kids_feed_asset(
        payload.asset_id,
        profile=profile,
        require_current_authorization=payload.event == "selected",
        minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
        minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
    )
    if asset is None:
        raise HTTPException(status_code=404, detail="Kids asset not found")
    if payload.session_id:
        lease_context = await runtime.db.kids_relay_lease_event_context(
            payload.session_id
        )
        if (
            lease_context is None
            or lease_context["item_id"] != asset["item_id"]
            or lease_context["feed_session_id"] != asset["feed_session_id"]
            or lease_context["profile"] != profile
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "kids_event_session_mismatch", "message": "Kids session does not match asset."},
            )
        if payload.event in {"started", "completed"}:
            await _kids_checked_lease(request, payload.session_id)
        elif payload.event == "stopped" and lease_context["state"] not in {
            "active",
            "revoked",
            "expired",
            "closed",
        }:
            raise HTTPException(
                status_code=409,
                detail={"code": "kids_event_session_inactive", "message": "Kids session state is invalid."},
            )
    elif payload.event != "selected":
        raise HTTPException(
            status_code=409,
            detail={"code": "kids_event_session_required", "message": "Kids playback events require a session."},
        )
    correlation_id = (
        payload.correlation_id
        or request.headers.get("X-Correlation-ID")
        or "kids-dataplane"
    )
    event = await runtime.db.kids_watch_event_record(
        video_id=asset["video_id"],
        event=payload.event,
        profile=profile,
        position_seconds=payload.position_seconds,
        session_id=payload.session_id,
        startup_ms=payload.startup_ms,
        correlation_id=correlation_id,
    )
    if event is None:
        raise HTTPException(status_code=404, detail="Kids catalog item not found")
    return {"status": "accepted", "event_id": event["id"]}


@router.get("/api/kids/catalog/items")
async def api_catalog_items(
    request: Request,
    profile: str = Query(default="noah", min_length=1, max_length=64),
) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    profile = (await _kids_profile(request, profile))["slug"]
    if await runtime.db.kids_kill_switch_enabled():
        return {"state": "kill_switch", "items": []}
    if not await runtime.monitoring_enabled_now():
        return {"state": "schedule_closed", "items": []}
    await runtime.reconcile_kids_catalog_policy()
    return {
        "state": "ready",
        "items": await runtime.db.kids_eligible_feed_list(
            runtime.settings.kids_playback_min_remaining_seconds,
            profile=profile,
        ),
    }


@router.get("/api/kids/catalog/items/{item_id}")
async def api_catalog_item(item_id: int, request: Request) -> dict[str, Any]:
    if await request.app.state.runtime.db.kids_kill_switch_enabled():
        raise HTTPException(status_code=403, detail="Kids kill switch is active")
    item = await request.app.state.runtime.db.catalog_get("item", item_id)
    if not item:
        raise HTTPException(status_code=404, detail="catalog item not found")
    return item


@router.get("/api/kids/catalog/items/by-video/{video_id}")
async def api_catalog_item_by_video(video_id: str, request: Request) -> dict[str, Any]:
    if await request.app.state.runtime.db.kids_kill_switch_enabled():
        raise HTTPException(status_code=403, detail="Kids kill switch is active")
    item = await request.app.state.runtime.db.catalog_item_by_video(video_id)
    if not item:
        raise HTTPException(status_code=404, detail="catalog item not found")
    return item


@router.get("/api/kids/sources")
async def api_catalog_sources(
    request: Request,
    state: str | None = Query(default=None),
    verdict: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    profile: str | None = Query(default=None),
    query: str | None = Query(default=None, max_length=200),
    sort: str = Query(default="id-desc", max_length=32),
    limit: int = Query(default=500, ge=1, le=1000),
) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    if profile:
        profile = (await _kids_profile(request, profile))["slug"]
    return {
        "sources": await runtime.db.catalog_sources_list(
            state=state,
            verdict=verdict,
            kind=kind,
            profile=profile,
            query=query,
            sort=sort,
            limit=limit,
        )
    }


@router.get("/api/kids/sources/{source_id}/poster-items")
async def api_kids_source_poster_items(
    source_id: int,
    request: Request,
) -> dict[str, Any]:
    items = await request.app.state.runtime.db.kids_source_poster_items(source_id)
    if items is None:
        raise HTTPException(status_code=404, detail="catalog source not found")
    return {"source_id": source_id, "items": items}


@router.put("/api/kids/sources/{source_id}/poster")
async def api_kids_source_poster(
    source_id: int,
    payload: KidsSourcePosterRequest,
    request: Request,
) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    await runtime.reconcile_kids_catalog_policy()
    try:
        result = await runtime.db.kids_source_poster_set(
            source_id,
            payload.item_id,
            actor=payload.actor,
            reason=payload.reason,
            correlation_id=payload.correlation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="catalog source not found")
    return _kids_source_poster_response(result)


@router.put("/api/kids/sources/{source_id}/classification")
async def api_kids_source_classification(
    source_id: int,
    payload: KidsSourceClassificationRequest,
    request: Request,
) -> dict[str, Any]:
    result = await request.app.state.runtime.db.catalog_source_classification_update(
        source_id,
        language=payload.language,
        content_kind=payload.content_kind,
        age_suitability=payload.age_suitability,
        actor=payload.actor,
        reason=payload.reason,
        correlation_id=payload.correlation_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="catalog source not found")
    return result


@router.post("/api/kids/sources")
async def api_catalog_source(payload: CatalogSourceRequest, request: Request) -> dict[str, Any]:
    correlation_id = request.headers.get("X-Correlation-ID", f"catalog-source-{payload.reference}")
    try:
        return await request.app.state.runtime.db.catalog_create("source", {**payload.model_dump(), "correlation_id": correlation_id})
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/kids/profiles")
async def api_kids_profiles(request: Request) -> dict[str, Any]:
    profiles = await request.app.state.runtime.db.kids_profiles_list()
    return {"profiles": [kids_profile_payload(request, profile) for profile in profiles]}


@router.get("/api/kids/profiles/{profile}/avatar")
async def api_kids_profile_avatar(profile: str, request: Request) -> FileResponse:
    profile = (await _kids_profile(request, profile, require_enabled=False))["slug"]
    avatar_path = _kids_profile_avatar_path(request, profile)
    if not avatar_path.is_file():
        raise HTTPException(status_code=404, detail="Profile image not found")
    media_type = _kids_profile_avatar_media_type(avatar_path.read_bytes()[:32])
    if media_type is None:
        raise HTTPException(status_code=404, detail="Profile image is invalid")
    return FileResponse(
        avatar_path,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.put("/api/kids/profiles/{profile}/avatar")
async def api_kids_profile_avatar_update(profile: str, request: Request) -> dict[str, Any]:
    profile_row = await _kids_profile(request, profile, require_enabled=False)
    data = await _kids_profile_avatar_body(request)
    media_type = _kids_profile_avatar_media_type(data)
    if media_type is None:
        raise HTTPException(status_code=415, detail="Use a JPEG, PNG, WebP, HEIC, HEIF, or AVIF image")

    avatar_path = _kids_profile_avatar_path(request, profile_row["slug"])
    avatar_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = avatar_path.with_name(
        f".{profile_row['slug']}.{secrets.token_hex(8)}.tmp"
    )
    try:
        temporary_path.write_bytes(data)
        os.replace(temporary_path, avatar_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    await request.app.state.runtime.db.audit_kids_event(
        event="profile_avatar_updated",
        entity_type="profile",
        actor="parent-ui",
        reason=f"profile={profile_row['slug']} media_type={media_type} bytes={len(data)}",
        correlation_id=request.headers.get(
            "X-Correlation-ID",
            f"profile-avatar-{profile_row['slug']}",
        ),
    )
    return {"profile": kids_profile_payload(request, profile_row)}


@router.delete("/api/kids/profiles/{profile}/avatar")
async def api_kids_profile_avatar_delete(profile: str, request: Request) -> dict[str, Any]:
    profile_row = await _kids_profile(request, profile, require_enabled=False)
    avatar_path = _kids_profile_avatar_path(request, profile_row["slug"])
    existed = avatar_path.is_file()
    avatar_path.unlink(missing_ok=True)
    if existed:
        await request.app.state.runtime.db.audit_kids_event(
            event="profile_avatar_deleted",
            entity_type="profile",
            actor="parent-ui",
            reason=f"profile={profile_row['slug']}",
            correlation_id=request.headers.get(
                "X-Correlation-ID",
                f"profile-avatar-{profile_row['slug']}",
            ),
        )
    return {"profile": kids_profile_payload(request, profile_row)}


@router.put("/api/kids/sources/{source_id}/profiles")
async def api_kids_source_profiles(
    source_id: int,
    payload: KidsSourceProfilesRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        result = await request.app.state.runtime.db.kids_source_profiles_set(
            source_id,
            payload.profile_slugs,
            actor=payload.actor,
            reason=payload.reason,
            correlation_id=payload.correlation_id,
            persist_parent_selection=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="catalog source not found")
    return result


@router.get("/api/kids/resolve")
async def api_kids_resolve(
    request: Request,
    status: str | None = Query(default=None),
    profile: str | None = Query(default=None),
    query: str | None = Query(default=None, max_length=200),
    sort: str = Query(default="updated-desc", max_length=32),
    limit: int = Query(default=500, ge=1, le=500),
) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    if profile:
        profile = (await _kids_profile(request, profile))["slug"]
    return {
        "summary": await runtime.db.kids_resolve_summary(
            minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
            profile=profile,
        ),
        "rows": await runtime.db.kids_resolve_recent_rows(
            limit,
            status=status,
            profile=profile,
            query=query,
            sort=sort,
        ),
    }


@router.post("/api/kids/catalog/items")
async def api_catalog_item_create(payload: CatalogItemRequest, request: Request) -> dict[str, Any]:
    correlation_id = request.headers.get("X-Correlation-ID", f"catalog-item-{payload.video_id}")
    try:
        return await request.app.state.runtime.db.catalog_create("item", {**payload.model_dump(), "correlation_id": correlation_id})
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.patch("/api/kids/sources/{source_id}/state")
async def api_catalog_source_state(source_id: int, payload: CatalogTransitionRequest, request: Request) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    if payload.state == "approved":
        source = await runtime.db.catalog_get("source", source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="catalog source not found")
        decision = await runtime.judge.match_catalog_source_blocklist(source)
        if decision:
            raise HTTPException(
                status_code=409,
                detail="Cannot approve a source blocked by /blocklist",
            )
    try:
        result = await runtime.db.catalog_transition("source", source_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="catalog source not found")
    return result


@router.patch("/api/kids/catalog/items/{item_id}/state")
async def api_catalog_item_state(item_id: int, payload: CatalogTransitionRequest, request: Request) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    if payload.state == "approved":
        item = await runtime.db.catalog_get("item", item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="catalog item not found")
        source = (
            await runtime.db.catalog_get("source", int(item["source_id"]))
            if item.get("source_id") is not None
            else None
        )
        decision = await runtime.judge.match_catalog_item_blocklist(item, source)
        if decision:
            raise HTTPException(
                status_code=409,
                detail="Cannot approve an item blocked by /blocklist",
            )
    try:
        result = await runtime.db.catalog_transition("item", item_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="catalog item not found")
    return result


@router.get("/api/kids/status")
async def api_kids_status(request: Request) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    return {
        "kill_switch": await runtime.db.kids_kill_switch_enabled(),
        "catalog_revision": await runtime.db.catalog_revision(),
        "available": not await runtime.db.kids_kill_switch_enabled() and await runtime.monitoring_enabled_now(),
        "schedule_active": bool((await runtime.current_schedule_context()).get("active", False)),
        "monitoring_effective": await runtime.monitoring_enabled_now(),
        "resolve": await runtime.db.kids_resolve_summary(
            minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds
        ),
        "profiles": await runtime.db.kids_profiles_list(),
        "resolver_last_success_at": await runtime.db.get_setting("kids_resolver_last_success_at"),
    }


@router.get("/api/kids/readyz")
@router.get("/readyz")
async def api_kids_readyz(request: Request) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    last_success = await runtime.db.get_setting("kids_ingest_last_success_at")
    ingest_age_seconds: int | None = None
    ingest_fresh = False
    if last_success:
        try:
            finished_at = datetime.fromisoformat(last_success)
            if finished_at.tzinfo is None:
                finished_at = finished_at.replace(tzinfo=timezone.utc)
            ingest_age_seconds = max(
                0,
                int((datetime.now(timezone.utc) - finished_at).total_seconds()),
            )
            ingest_fresh = ingest_age_seconds <= runtime.settings.kids_ingest_freshness_seconds
        except ValueError:
            ingest_age_seconds = None

    opencodex_status = (
        await runtime.db.get_setting("kids_resolver_classifier_status")
    ) or "unavailable"
    opencodex_ready = opencodex_status == "ready"

    resolve_summary = await runtime.db.kids_resolve_summary(
        minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
        minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
    )
    payload = {
        "status": (
            "ready"
            if opencodex_ready
            and resolve_summary["fresh_ready"] >= runtime.settings.kids_ready_minimum
            else "unready"
        ),
        "opencodex": "ready" if opencodex_ready else "unavailable",
        "opencodex_model": runtime.settings.opencodex_model,
        "ingest": "fresh" if ingest_fresh else "stale",
        "ingest_last_success_at": last_success,
        "ingest_age_seconds": ingest_age_seconds,
        "catalog_revision": await runtime.db.catalog_revision(),
        "resolver_last_success_at": await runtime.db.get_setting("kids_resolver_last_success_at"),
    }
    payload["backlog_counts"] = resolve_summary["counts"]
    payload["fresh_ready_count"] = resolve_summary["fresh_ready"]
    payload["ready_minimum"] = runtime.settings.kids_ready_minimum
    if payload["status"] != "ready":
        raise HTTPException(status_code=503, detail=payload)
    return payload


@router.get("/api/kids/control/kill-switch")
async def api_kids_kill_switch(request: Request) -> dict[str, Any]:
    return {"enabled": await request.app.state.runtime.db.kids_kill_switch_enabled()}


@router.post("/api/kids/control/kill-switch")
async def api_kids_set_kill_switch(payload: KidsKillSwitchRequest, request: Request) -> dict[str, Any]:
    ok = await request.app.state.runtime.db.set_kids_kill_switch(
        enabled=payload.enabled,
        actor=payload.actor,
        reason=payload.reason,
        correlation_id=payload.correlation_id,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Kids kill switch state was not persisted")
    return {"enabled": payload.enabled}


@router.get("/api/kids/audit")
async def api_kids_audit(request: Request, limit: int = 100) -> dict[str, Any]:
    return {"events": await request.app.state.runtime.db.kids_audit_events(limit)}


@router.post("/api/kids/watch-events", status_code=202)
async def api_kids_watch_event(
    payload: KidsWatchEventRequest,
    request: Request,
) -> dict[str, Any]:
    profile = (await _kids_profile(request, payload.profile))["slug"]
    event = await request.app.state.runtime.db.kids_watch_event_record(
        video_id=payload.video_id,
        event=payload.event,
        profile=profile,
        position_seconds=payload.position_seconds,
        session_id=payload.session_id,
        startup_ms=payload.startup_ms,
        correlation_id=payload.correlation_id,
    )
    if event is None:
        raise HTTPException(status_code=404, detail="catalog item not found")
    return {"status": "accepted", "event_id": event["id"]}


@router.get("/api/kids/watch-events")
async def api_kids_watch_events(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
    profile: str | None = Query(default=None),
    query: str | None = Query(default=None, max_length=200),
    sort: str = Query(default="id-desc", max_length=32),
) -> dict[str, Any]:
    if profile:
        profile = (await _kids_profile(request, profile))["slug"]
    return {
        "events": await request.app.state.runtime.db.kids_watch_events_list(
            limit,
            profile=profile,
            query=query,
            sort=sort,
        )
    }


@router.get("/api/kids/playback-authorizations/{video_id}")
async def api_kids_playback_authorization(
    video_id: str,
    request: Request,
    include_candidate: bool = True,
    profile: str = Query(default="noah", min_length=1, max_length=64),
) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    profile = (await _kids_profile(request, profile))["slug"]
    if await runtime.db.kids_kill_switch_enabled() or not await runtime.monitoring_enabled_now():
        raise HTTPException(status_code=403, detail="Kids playback is unavailable")
    await runtime.reconcile_kids_catalog_policy()
    if include_candidate:
        row = await runtime.db.kids_playback_authorization(
            video_id,
            profile=profile,
            minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
        )
    else:
        row = await runtime.db.kids_playback_policy_authorization(
            video_id,
            profile=profile,
        )
    if row is None:
        raise HTTPException(status_code=403, detail="Kids playback is not authorized")
    response = {
        "status": "approved",
        "catalog_revision": await runtime.db.catalog_revision(),
        "item_id": row["item_id"],
        "video_id": row["video_id"],
    }
    if include_candidate:
        response.update(
            {
                "expires_at": row["expires_at"],
                "quality_height": row["quality_height"],
                "codec": row["codec"],
                "candidate": row["candidate"],
            }
        )
    return response
