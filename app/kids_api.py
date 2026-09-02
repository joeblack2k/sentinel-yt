from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any, AsyncGenerator
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from .models import (
    CatalogItemRequest,
    CatalogSourceRequest,
    CatalogTransitionRequest,
    KidsDataplaneEventRequest,
    KidsKillSwitchRequest,
    KidsPlaybackSessionRequest,
    KidsWatchEventRequest,
)
from .services.kids_classifier import OpenCodexKidsClassifier


KIDS_DATAPLANE_POLICY_VERSION = "sentinel-kids-v1"
router = APIRouter()


@router.get("/api/kids/catalog/revision")
async def api_catalog_revision(request: Request) -> dict[str, Any]:
    return {"revision": await request.app.state.runtime.db.catalog_revision()}


def _encode_kids_cursor(session_id: str, offset: int) -> str:
    payload = json.dumps([session_id, int(offset)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_kids_cursor(value: str) -> tuple[str, int]:
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
            or len(payload) != 2
            or not isinstance(payload[0], str)
            or not payload[0]
            or len(payload[0]) > 128
            or type(payload[1]) is not int
            or payload[1] < 0
        ):
            raise ValueError
        return payload[0], payload[1]
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


def _kids_lease_failure(result: dict[str, Any]) -> HTTPException:
    failure = result.get("status", "ineligible")
    if failure == "not_found":
        status_code = 404
    elif failure in {"expired", "policy_mismatch", "stale_revision"}:
        status_code = 409
    else:
        status_code = 403
    return HTTPException(
        status_code=status_code,
        detail={"code": f"kids_{failure}", "message": "Kids playback is not authorized."},
    )


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
    return headers


def _kids_upstream_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: response.headers[name]
        for name in ("accept-ranges", "content-length", "content-range", "content-type")
        if name in response.headers
    }


@router.get("/v1/kids/feed")
async def kids_feed(
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=36, ge=1, le=60),
    profile: str = Query(default="noah", min_length=1, max_length=64),
) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    await runtime.reconcile_kids_catalog_policy()
    state = await runtime.kids_policy_state()
    if cursor:
        session_id, offset = _decode_kids_cursor(cursor)
    else:
        session = await runtime.db.kids_feed_session_create(
            profile=profile,
            policy_version=KIDS_DATAPLANE_POLICY_VERSION,
            minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
            minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
            include_items=state == "ready",
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
            _encode_kids_cursor(session_id, next_offset)
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
    if asset["catalog_revision"] != await runtime.db.catalog_revision():
        raise HTTPException(status_code=409, detail="Kids asset is stale")
    parsed = urlsplit(str(asset["thumbnail_url"]))
    if parsed.scheme != "https" or parsed.hostname != "i.ytimg.com":
        raise HTTPException(status_code=404, detail="Kids thumbnail is unavailable")
    try:
        upstream = await runtime.kids_http_client.get(str(asset["thumbnail_url"]))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Kids thumbnail upstream unavailable") from exc
    content_type = upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if upstream.status_code != 200 or not content_type.startswith("image/"):
        await upstream.aclose()
        raise HTTPException(status_code=502, detail="Kids thumbnail upstream unavailable")
    content = upstream.content
    await upstream.aclose()
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
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
    runtime, lease = await _kids_checked_lease(request, lease_id)
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
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Kids media upstream unavailable") from exc
    response_headers = _kids_upstream_headers(upstream)
    if request.method == "HEAD" or upstream.status_code >= 400:
        status_code = upstream.status_code
        await upstream.aclose()
        return Response(status_code=status_code, headers=response_headers)

    async def body() -> AsyncGenerator[bytes, None]:
        try:
            async for chunk in upstream.aiter_bytes():
                if not await _kids_checked_stream(
                    runtime,
                    lease_id,
                    minimum_quality_height=runtime.settings.kids_resolver_min_quality_height,
                ):
                    break
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
    asset = await runtime.db.kids_feed_asset(
        payload.asset_id,
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
            or lease_context["profile"] != payload.profile
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
        profile=payload.profile,
        position_seconds=payload.position_seconds,
        session_id=payload.session_id,
        startup_ms=payload.startup_ms,
        correlation_id=correlation_id,
    )
    if event is None:
        raise HTTPException(status_code=404, detail="Kids catalog item not found")
    return {"status": "accepted", "event_id": event["id"]}


@router.get("/api/kids/catalog/items")
async def api_catalog_items(request: Request) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    if await runtime.db.kids_kill_switch_enabled():
        return {"state": "kill_switch", "items": []}
    if not await runtime.monitoring_enabled_now():
        return {"state": "schedule_closed", "items": []}
    await runtime.reconcile_kids_catalog_policy()
    return {
        "state": "ready",
        "items": await runtime.db.kids_eligible_feed_list(runtime.settings.kids_playback_min_remaining_seconds),
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
async def api_catalog_sources(request: Request) -> dict[str, Any]:
    return {"sources": await request.app.state.runtime.db.catalog_sources_list()}


@router.post("/api/kids/sources")
async def api_catalog_source(payload: CatalogSourceRequest, request: Request) -> dict[str, Any]:
    correlation_id = request.headers.get("X-Correlation-ID", f"catalog-source-{payload.reference}")
    try:
        return await request.app.state.runtime.db.catalog_create("source", {**payload.model_dump(), "correlation_id": correlation_id})
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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

    opencodex_ready = False
    classifier = OpenCodexKidsClassifier(
        base_url=runtime.settings.opencodex_base_url,
        model=runtime.settings.opencodex_model,
    )
    try:
        await classifier.check_model()
        opencodex_ready = True
    except Exception:
        opencodex_ready = False
    finally:
        await classifier.close()

    payload = {
        "status": "ready" if opencodex_ready and ingest_fresh else "unready",
        "opencodex": "ready" if opencodex_ready else "unavailable",
        "opencodex_model": runtime.settings.opencodex_model,
        "ingest": "fresh" if ingest_fresh else "stale",
        "ingest_last_success_at": last_success,
        "ingest_age_seconds": ingest_age_seconds,
        "catalog_revision": await runtime.db.catalog_revision(),
        "resolver_last_success_at": await runtime.db.get_setting("kids_resolver_last_success_at"),
    }
    resolve_summary = await runtime.db.kids_resolve_summary(
        minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds
    )
    payload["backlog_counts"] = resolve_summary["counts"]
    payload["fresh_ready_count"] = resolve_summary["fresh_ready"]
    payload["ready_minimum"] = runtime.settings.kids_ready_minimum
    if resolve_summary["fresh_ready"] < runtime.settings.kids_ready_minimum:
        payload["status"] = "unready"
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
    event = await request.app.state.runtime.db.kids_watch_event_record(
        video_id=payload.video_id,
        event=payload.event,
        profile=payload.profile,
        position_seconds=payload.position_seconds,
        session_id=payload.session_id,
        startup_ms=payload.startup_ms,
        correlation_id=payload.correlation_id,
    )
    if event is None:
        raise HTTPException(status_code=404, detail="catalog item not found")
    return {"status": "accepted", "event_id": event["id"]}


@router.get("/api/kids/watch-events")
async def api_kids_watch_events(request: Request, limit: int = 100) -> dict[str, Any]:
    return {"events": await request.app.state.runtime.db.kids_watch_events_list(limit)}


@router.get("/api/kids/playback-authorizations/{video_id}")
async def api_kids_playback_authorization(
    video_id: str,
    request: Request,
    include_candidate: bool = True,
) -> dict[str, Any]:
    runtime: Any = request.app.state.runtime
    if await runtime.db.kids_kill_switch_enabled() or not await runtime.monitoring_enabled_now():
        raise HTTPException(status_code=403, detail="Kids playback is unavailable")
    await runtime.reconcile_kids_catalog_policy()
    if include_candidate:
        row = await runtime.db.kids_playback_authorization(
            video_id,
            minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
        )
    else:
        row = await runtime.db.kids_playback_policy_authorization(video_id)
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
