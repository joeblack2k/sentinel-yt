from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, AsyncGenerator

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import POLICY_PRESETS, SUPPORTED_TIMEZONES, Settings
from .db import Database, utc_now_iso
from .kids_api import kids_profile_payload, router as kids_router
from .models import (
    ControlStateRequest,
    LocalBlocklistContentRequest,
    PolicyFlagsRequest,
    PurgeRequest,
    RuleRequest,
    RulesImportSourcesRequest,
    ScheduleRequest,
    ScheduleWindowRequest,
    WebhookControlRequest,
    WebhookSettingsRequest,
)
from .services.blocklists import BlocklistService
from .services.judge import JudgeService, normalize_policy_flags
from .services.scheduler import ScheduleService

logger = logging.getLogger("sentinel")

@dataclass
class RuntimeState:
    settings: Settings
    db: Database
    judge: JudgeService
    blocklists: BlocklistService
    kids_http_client: httpx.AsyncClient
    kids_reconcile_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    kids_reconciled_at: float = 0.0
    kids_reconciled_blocklist_loaded_at: str = ""

    async def reconcile_kids_catalog_policy(self, *, force: bool = False) -> int:
        now = monotonic()
        blocklist_loaded_at = self.blocklists.summary().get("loaded_at", "")
        if (
            not force
            and now - self.kids_reconciled_at < 30
            and blocklist_loaded_at == self.kids_reconciled_blocklist_loaded_at
        ):
            return 0
        async with self.kids_reconcile_lock:
            now = monotonic()
            blocklist_loaded_at = self.blocklists.summary().get("loaded_at", "")
            if (
                not force
                and now - self.kids_reconciled_at < 30
                and blocklist_loaded_at == self.kids_reconciled_blocklist_loaded_at
            ):
                return 0
            blocked = await self.judge.reconcile_catalog_policy()
            self.kids_reconciled_at = monotonic()
            self.kids_reconciled_blocklist_loaded_at = self.blocklists.summary().get("loaded_at", "")
            return blocked

    async def get_status(self) -> dict[str, Any]:
        settings = await self.db.all_settings()
        schedule_ctx = await self.current_schedule_context(settings_map=settings)
        schedule_active_now = bool(schedule_ctx.get("active", True))
        schedule_mode_now = str(schedule_ctx.get("mode", "blocklist"))
        schedule_timezone = str(schedule_ctx.get("timezone", settings.get("timezone", "UTC")))
        schedules_count = int(schedule_ctx.get("schedules_count", 0))
        monitoring_effective = await self.monitoring_enabled_now(settings)
        return {
            "active": settings.get("active", "true") == "true",
            "monitoring_effective": monitoring_effective,
            "schedule_active_now": schedule_active_now,
            "schedule_mode_now": schedule_mode_now,
            "schedules_count": schedules_count,
            "timezone": schedule_timezone,
            "judge_ok": settings.get("judge_ok", "true") == "true",
            "last_error": settings.get("last_error", ""),
            "build_version": self.settings.build_version,
        }

    async def current_schedule_context(self, settings_map: dict[str, str] | None = None) -> dict[str, Any]:
        settings = settings_map or await self.db.all_settings()
        schedules = await self.db.list_schedules()
        if schedules:
            active_row = ScheduleService.pick_active_window(schedules)
            if active_row:
                return {
                    "active": True,
                    "mode": active_row.get("mode", "blocklist"),
                    "timezone": active_row.get("timezone", settings.get("timezone", "UTC")),
                    "schedule_id": active_row.get("id"),
                    "schedule_name": active_row.get("name", ""),
                    "schedules_count": len(schedules),
                }
            return {
                "active": False,
                "mode": "blocklist",
                "timezone": settings.get("timezone", "UTC"),
                "schedule_id": None,
                "schedule_name": "",
                "schedules_count": len(schedules),
            }

        schedule_enabled = settings.get("schedule_enabled", "true") == "true"
        schedule_start = settings.get("schedule_start", "07:00")
        schedule_end = settings.get("schedule_end", "19:00")
        timezone_name = settings.get("timezone", "UTC")
        schedule_active = ScheduleService.is_active(
            enabled=schedule_enabled,
            start=schedule_start,
            end=schedule_end,
            timezone_name=timezone_name,
        )
        return {
            "active": schedule_active,
            "mode": settings.get("schedule_mode", "blocklist"),
            "timezone": timezone_name,
            "schedule_id": None,
            "schedule_name": "Legacy",
            "schedules_count": 0,
        }

    async def monitoring_enabled_now(self, settings_map: dict[str, str] | None = None) -> bool:
        settings = settings_map or await self.db.all_settings()
        active = settings.get("active", "true") == "true"
        schedule_ctx = await self.current_schedule_context(settings_map=settings)
        return active and bool(schedule_ctx.get("active", True))

    async def kids_policy_state(self) -> str:
        if await self.db.kids_kill_switch_enabled():
            await self.db.kids_revoke_active_leases(reason="kill_switch")
            return "kill_switch"
        settings = await self.db.all_settings()
        if settings.get("active", "true") != "true":
            await self.db.kids_revoke_active_leases(reason="monitoring_disabled")
            return "schedule_closed"
        schedule_ctx = await self.current_schedule_context(settings_map=settings)
        if not schedule_ctx.get("active", True):
            await self.db.kids_revoke_active_leases(reason="schedule_closed")
            return "schedule_closed"
        return "ready"

    async def _set_bool_setting_confirmed(self, key: str, value: bool) -> None:
        target = "true" if value else "false"
        persisted = False
        for _ in range(3):
            await self.db.set_setting(key, target)
            if await self.db.get_setting(key) == target:
                persisted = True
                break
            await asyncio.sleep(0.05)
        if not persisted:
            raise RuntimeError(f'Failed to persist setting "{key}" as {target}.')

    async def set_monitoring_active(self, active: bool) -> None:
        await self._set_bool_setting_confirmed("active", active)
        logger.info("monitoring_active updated to %s", active)
        if not active:
            await self.db.kids_revoke_active_leases(reason="monitoring_disabled")
        await self.db.set_setting("last_error", "")

settings = Settings()
db = Database(settings.db_path)
blocklists = BlocklistService(settings)
judge = JudgeService(db, blocklists=blocklists)


def _new_kids_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=20.0, follow_redirects=False)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await db.init()
    await db.kids_resolve_sync_backlog(
        minimum_quality_height=settings.kids_resolver_min_quality_height,
    )
    await blocklists.reload(db)
    kids_http_client = _new_kids_http_client()
    runtime = RuntimeState(
        settings=settings,
        db=db,
        judge=judge,
        blocklists=blocklists,
        kids_http_client=kids_http_client,
    )
    app.state.runtime = runtime
    try:
        await runtime.reconcile_kids_catalog_policy(force=True)
        yield
    finally:
        await kids_http_client.aclose()


app = FastAPI(title="Sentinel", lifespan=lifespan)
base_dir = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(base_dir / "templates"))
app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")
app.include_router(kids_router)


@app.middleware("http")
async def cache_control_middleware(request: Request, call_next):
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if "text/html" in content_type:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def page_home(request: Request) -> HTMLResponse:
    return RedirectResponse(url="/kids", status_code=307)


@app.get("/history", response_class=HTMLResponse)
async def page_history(request: Request) -> HTMLResponse:
    runtime: RuntimeState = request.app.state.runtime
    kids_watch_events = await runtime.db.kids_watch_events_list(limit=100)
    status = await runtime.get_status()
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "kids_watch_events": kids_watch_events,
            "profiles": await runtime.db.kids_profiles_list(),
            "status": status,
            "page": "history",
        },
    )


@app.get("/sources", response_class=HTMLResponse)
async def page_sources(
    request: Request,
    state: str | None = Query(default=None),
    verdict: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    profile: str | None = Query(default=None),
    query: str | None = Query(default=None, max_length=200),
    sort: str = Query(default="id-desc", max_length=32),
) -> HTMLResponse:
    runtime: RuntimeState = request.app.state.runtime
    if profile and await runtime.db.kids_profile_get(profile) is None:
        raise HTTPException(status_code=404, detail="Kids profile not found")
    return templates.TemplateResponse(
        request,
        "sources.html",
        {
            "sources": await runtime.db.catalog_sources_list(
                state=state,
                verdict=verdict,
                kind=kind,
                profile=profile,
                query=query,
                sort=sort,
            ),
            "profiles": await runtime.db.kids_profiles_list(),
            "status": await runtime.get_status(),
            "page": "sources",
        },
    )


@app.get("/resolve", response_class=HTMLResponse)
async def page_resolve(
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    profile: str | None = Query(default=None),
    query: str | None = Query(default=None, max_length=200),
    sort: str = Query(default="updated-desc", max_length=32),
) -> HTMLResponse:
    runtime: RuntimeState = request.app.state.runtime
    if profile and await runtime.db.kids_profile_get(profile) is None:
        raise HTTPException(status_code=404, detail="Kids profile not found")
    resolve_summary = await runtime.db.kids_resolve_summary(
        minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds,
        profile=profile,
    )
    return templates.TemplateResponse(
        request,
        "resolve.html",
        {
            "kids_status": {
                "resolve": resolve_summary,
                "resolver_last_success_at": await runtime.db.get_setting("kids_resolver_last_success_at"),
                "catalog_revision": await runtime.db.catalog_revision(),
            },
            "resolve_rows": await runtime.db.kids_resolve_recent_rows(
                limit=500,
                status=status_filter,
                profile=profile,
                query=query,
                sort=sort,
            ),
            "profiles": await runtime.db.kids_profiles_list(),
            "status": await runtime.get_status(),
            "page": "resolve",
        },
    )


@app.get("/blocklist", response_class=HTMLResponse)
async def page_blocklist(request: Request) -> HTMLResponse:
    runtime: RuntimeState = request.app.state.runtime
    rules = await runtime.db.list_rules(limit=200, rule_type="blacklist")
    settings_map = await runtime.db.all_settings()
    policy_flags = normalize_policy_flags(settings_map.get("policy_flags_json", "{}"))
    status = await runtime.get_status()
    blocklist_summary = runtime.blocklists.summary()
    sources = await runtime.blocklists.get_sources(runtime.db)
    local_blocklist = await runtime.blocklists.get_local_content()
    return templates.TemplateResponse(
        request,
        "blocklist.html",
        {
            "rules": rules,
            "blocklist_summary": blocklist_summary,
            "blocklist_sources": sources,
            "local_blocklist_content": local_blocklist,
            "status": status,
            "policy_presets": POLICY_PRESETS,
            "policy_flags": policy_flags,
            "page": "blocklist",
        },
    )


@app.get("/schedule", response_class=HTMLResponse)
async def page_schedule(request: Request) -> HTMLResponse:
    runtime: RuntimeState = request.app.state.runtime
    schedules = await runtime.db.list_schedules()
    status = await runtime.get_status()
    return templates.TemplateResponse(
        request,
        "schedule.html",
        {
            "schedules": schedules,
            "status": status,
            "timezones": SUPPORTED_TIMEZONES,
            "page": "schedule",
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request) -> HTMLResponse:
    runtime: RuntimeState = request.app.state.runtime
    status = await runtime.get_status()
    settings_map = await runtime.db.all_settings()
    db_stats = await runtime.db.db_stats()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "status": status,
            "settings": settings_map,
            "opencodex_base_url": runtime.settings.opencodex_base_url,
            "opencodex_model": runtime.settings.opencodex_model,
            "timezones": SUPPORTED_TIMEZONES,
            "db_stats": db_stats,
            "page": "settings",
        },
    )


@app.get("/kids", response_class=HTMLResponse)
async def page_kids(request: Request) -> HTMLResponse:
    runtime: RuntimeState = request.app.state.runtime
    profiles = await runtime.db.kids_profiles_list()
    return templates.TemplateResponse(
        request,
        "kids.html",
        {
            "status": await runtime.get_status(),
            "kids_status": {
                "kill_switch": await runtime.db.kids_kill_switch_enabled(),
                "catalog_revision": await runtime.db.catalog_revision(),
                "resolve": await runtime.db.kids_resolve_summary(
                    minimum_remaining_seconds=runtime.settings.kids_playback_min_remaining_seconds
                ),
                "resolver_last_success_at": await runtime.db.get_setting("kids_resolver_last_success_at"),
            },
            "sources": await runtime.db.catalog_sources_list(),
            "items": await runtime.db.catalog_item_list_all(),
            "watch_events": await runtime.db.kids_watch_events_list(),
            "resolve_rows": await runtime.db.kids_resolve_recent_rows(),
            "profiles": [kids_profile_payload(request, profile) for profile in profiles],
            "page": "kids",
        },
    )


@app.post("/api/control/state")
async def api_control_state(payload: ControlStateRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.set_monitoring_active(payload.active)
    status = await runtime.get_status()
    return {
        "active": status["active"],
        "monitoring_effective": status["monitoring_effective"],
        "changed_at": utc_now_iso(),
        "reason": "manual",
    }


@app.get("/api/status")
async def api_status(request: Request) -> dict[str, Any]:
    status = await request.app.state.runtime.get_status()
    status["kids_kill_switch"] = await request.app.state.runtime.db.kids_kill_switch_enabled()
    return status


@app.post("/api/webhook/control")
async def api_webhook_control(payload: WebhookControlRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.set_monitoring_active(payload.active)
    status = await runtime.get_status()
    return {
        "ok": True,
        "active": status["active"],
        "monitoring_effective": status["monitoring_effective"],
        "source": payload.source,
    }


@app.post("/api/blocklist/rules")
async def api_blacklist(payload: RuleRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    value = payload.video_id if payload.scope == "video" else payload.channel_id
    if not value:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "value_missing",
                "message": "Missing rule value. Provide a video ID for video scope, or a channel ID for channel scope.",
            },
        )
    label = (payload.label or "").strip()
    url = (payload.url or "").strip()
    await runtime.db.add_rule(
        "blacklist",
        payload.scope,
        value,
        label=label,
        url=url,
        source_list="manual",
    )
    await runtime.blocklists.append_entry(
        scope=payload.scope,
        value=value,
        label=label,
        url=url,
        source_list="manual",
    )
    blocked = await runtime.reconcile_kids_catalog_policy(force=True)
    return {"ok": True, "blocked": blocked}


@app.post("/api/blocklist/policies")
async def api_rules_policies(payload: PolicyFlagsRequest, request: Request) -> dict[str, Any]:
    flags = normalize_policy_flags(payload.flags)
    runtime: RuntimeState = request.app.state.runtime
    await runtime.db.set_setting("policy_flags_json", json.dumps(flags))
    await runtime.reconcile_kids_catalog_policy(force=True)
    return {"ok": True, "flags": flags}


@app.post("/api/blocklist/sources")
async def api_rules_blocklists_sources(payload: RulesImportSourcesRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.blocklists.set_sources(runtime.db, payload.urls)
    summary = await runtime.blocklists.reload(runtime.db)
    blocked = await runtime.reconcile_kids_catalog_policy(force=True)
    return {"ok": True, "summary": summary, "blocked": blocked, "sources": payload.urls}


@app.post("/api/blocklist/reload")
async def api_rules_blocklists_reload(request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    summary = await runtime.blocklists.reload(runtime.db)
    blocked = await runtime.reconcile_kids_catalog_policy(force=True)
    return {"ok": True, "summary": summary, "blocked": blocked}


@app.post("/api/blocklist/local")
async def api_rules_blocklists_local(payload: LocalBlocklistContentRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.blocklists.save_local_content(payload.content)
    summary = await runtime.blocklists.reload(runtime.db)
    blocked = await runtime.reconcile_kids_catalog_policy(force=True)
    return {"ok": True, "summary": summary, "blocked": blocked}


@app.delete("/api/blocklist/rules/{rule_id}")
async def api_rule_delete(rule_id: int, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    row = await runtime.db.get_rule(rule_id)
    await runtime.db.delete_rule(rule_id)
    if row and row.get("source_list") == "manual":
        if row.get("rule_type") == "blacklist":
            await runtime.blocklists.remove_entry(scope=row.get("scope", ""), value=row.get("value", ""))
            await runtime.blocklists.reload(runtime.db)
            await runtime.reconcile_kids_catalog_policy(force=True)
    return {"ok": True}


@app.get("/api/schedules")
async def api_schedules_list(request: Request) -> dict[str, Any]:
    rows = await request.app.state.runtime.db.list_schedules()
    return {"rows": rows, "count": len(rows)}


@app.post("/api/schedules/add")
async def api_schedule_add(payload: ScheduleWindowRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    schedule_id = await runtime.db.add_schedule(
        name=payload.name,
        enabled=payload.enabled,
        start=payload.start,
        end=payload.end,
        timezone=payload.timezone,
        mode=payload.mode,
    )
    await runtime.kids_policy_state()
    return {"ok": True, "id": schedule_id}


@app.post("/api/schedules/{schedule_id}/update")
async def api_schedule_update(schedule_id: int, payload: ScheduleWindowRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    updated = await runtime.db.update_schedule(
        schedule_id,
        name=payload.name,
        enabled=payload.enabled,
        start=payload.start,
        end=payload.end,
        timezone=payload.timezone,
        mode=payload.mode,
    )
    if not updated:
        raise HTTPException(status_code=404, detail={"code": "schedule_not_found", "message": "Schedule not found."})
    await runtime.kids_policy_state()
    return {"ok": True}


@app.delete("/api/schedules/{schedule_id}")
async def api_schedule_delete(schedule_id: int, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    schedules = await runtime.db.list_schedules()
    if len(schedules) <= 1:
        raise HTTPException(
            status_code=400,
            detail={"code": "schedule_minimum_one", "message": "At least one schedule must remain."},
        )
    deleted = await runtime.db.delete_schedule(schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"code": "schedule_not_found", "message": "Schedule not found."})
    await runtime.kids_policy_state()
    return {"ok": True}


@app.post("/api/settings/schedule")
async def api_settings_schedule(payload: ScheduleRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    # Legacy compatibility endpoint: update first schedule window in blocklist mode.
    schedules = await runtime.db.list_schedules()
    if schedules:
        first = schedules[0]
        await runtime.db.update_schedule(
            int(first["id"]),
            name=str(first.get("name") or "Default"),
            enabled=payload.enabled,
            start=payload.start,
            end=payload.end,
            timezone=payload.timezone,
            mode=str(first.get("mode") or "blocklist"),
        )
    else:
        await runtime.db.add_schedule(
            name="Default",
            enabled=payload.enabled,
            start=payload.start,
            end=payload.end,
            timezone=payload.timezone,
            mode="blocklist",
        )
    await runtime.db.set_setting("schedule_enabled", "true" if payload.enabled else "false")
    await runtime.db.set_setting("schedule_start", payload.start)
    await runtime.db.set_setting("schedule_end", payload.end)
    await runtime.db.set_setting("timezone", payload.timezone)
    await runtime.kids_policy_state()
    return {"ok": True}


@app.post("/api/settings/webhook")
async def api_settings_webhook(payload: WebhookSettingsRequest, request: Request) -> dict[str, Any]:
    await request.app.state.runtime.db.set_setting("failure_webhook_url", payload.failure_webhook_url.strip())
    return {"ok": True}


@app.get("/api/history")
async def api_history(request: Request) -> dict[str, Any]:
    events = await request.app.state.runtime.db.kids_watch_events_list(limit=100)
    return {"kids_watch_events": events}


@app.get("/api/db/stats")
async def api_db_stats(request: Request) -> dict[str, Any]:
    return await request.app.state.runtime.db.db_stats()


@app.post("/api/admin/purge")
async def api_admin_purge(payload: PurgeRequest, request: Request) -> dict[str, Any]:
    dbi = request.app.state.runtime.db
    deleted = await dbi.purge_history()
    stats = await dbi.db_stats()
    return {"ok": True, "target": payload.target, "deleted": deleted, "stats": stats}


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details: list[str] = []
    for err in exc.errors():
        field = ".".join([str(x) for x in err.get("loc", []) if x != "body"]) or "request"
        msg = err.get("msg", "Invalid value")
        details.append(f"{field}: {msg}")
    message = "Invalid request data. " + ("; ".join(details) if details else "Check your input and try again.")
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "detail": {"code": "validation_error", "message": message}},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled API error: %s", exc)
    try:
        await request.app.state.runtime.db.set_setting("last_error", f"{type(exc).__name__}: {exc}")
    except Exception:
        pass
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "detail": {
                "code": "internal_error",
                "message": "Unexpected server error. Please retry. If the issue continues, check the device status and logs.",
            },
        },
    )
