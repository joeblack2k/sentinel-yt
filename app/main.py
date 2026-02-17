from __future__ import annotations

import asyncio
import json
import logging
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from time import monotonic
from typing import Any, AsyncGenerator

import aiohttp
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import (
    ALLOW_POLICY_PRESETS,
    DEFAULT_SAFE_PROMPT,
    DEFAULT_SPONSORBLOCK_CATEGORIES,
    OUTPUT_CONTRACT_SUFFIX,
    POLICY_PRESETS,
    SUPPORTED_TIMEZONES,
    Settings,
)
from .db import Database, utc_now_iso
from .models import (
    AllowPolicyFlagsRequest,
    ControlStateRequest,
    GeminiSettingsRequest,
    LocalBlocklistContentRequest,
    MqttConfigRequest,
    MqttStateRequest,
    PairCodeOnlyRequest,
    PairDeviceRequest,
    PolicyFlagsRequest,
    PromptRequest,
    PurgeRequest,
    RuleRequest,
    RulesImportSourcesRequest,
    ScheduleRequest,
    ScheduleWindowRequest,
    SponsorBlockConfigRequest,
    SponsorBlockReleaseRequest,
    SponsorBlockScheduleRequest,
    SponsorBlockStateRequest,
    WebhookControlRequest,
    WebhookSettingsRequest,
)
from .services.blocklists import BlocklistService
from .services.discovery import DiscoveryService
from .services.judge import GeminiFatalError, JudgeService, normalize_allow_policy_flags, normalize_policy_flags
from .services.lounge_manager import LoungeManager, PairingError
from .services.mqtt_bridge import MQTTBridge
from .services.scheduler import ScheduleService
from .services.sponsorblock import SponsorBlockService
from .services.webhook import WebhookClient

logger = logging.getLogger("sentinel")


@dataclass
class RuntimeState:
    settings: Settings
    db: Database
    discovery: DiscoveryService
    webhook_client: WebhookClient
    judge: JudgeService
    lounge: LoungeManager
    blocklists: BlocklistService
    allowlists: BlocklistService
    sponsorblock: SponsorBlockService
    mqtt: MQTTBridge
    discovered_devices: list[dict[str, Any]] = field(default_factory=list)
    live_subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    supervisor_task: asyncio.Task[None] | None = None
    workers_enabled: bool = False
    up_next_repeat: dict[int, tuple[str, int]] = field(default_factory=dict)
    last_now_playing_at: dict[int, float] = field(default_factory=dict)
    last_now_playing_video: dict[int, tuple[str, float]] = field(default_factory=dict)
    block_retry_at: dict[str, float] = field(default_factory=dict)
    up_next_candidates: dict[int, list[str]] = field(default_factory=dict)
    reinforce_tasks: dict[int, asyncio.Task[None]] = field(default_factory=dict)
    last_history_choice: dict[int, str] = field(default_factory=dict)
    mqtt_publish_due_at: float = 0.0

    async def emit_live(self, payload: dict[str, Any]) -> None:
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in self.live_subscribers:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.live_subscribers.discard(q)

    async def get_status(self) -> dict[str, Any]:
        settings = await self.db.all_settings()
        counts = await self.db.counts()
        schedule_ctx = await self.current_schedule_context(settings_map=settings)
        schedule_active_now = bool(schedule_ctx.get("active", True))
        schedule_mode_now = str(schedule_ctx.get("mode", "blocklist"))
        schedule_timezone = str(schedule_ctx.get("timezone", settings.get("timezone", "UTC")))
        schedules_count = int(schedule_ctx.get("schedules_count", 0))
        monitoring_effective = await self.monitoring_enabled_now(settings)
        sponsorblock_effective = await self.sponsorblock_enabled_now(settings)
        return {
            "active": settings.get("active", "true") == "true",
            "monitoring_effective": monitoring_effective,
            "schedule_active_now": schedule_active_now,
            "schedule_mode_now": schedule_mode_now,
            "schedules_count": schedules_count,
            "sponsorblock_active": sponsorblock_effective,
            "sponsorblock_configured": settings.get("sponsorblock_active", "false") == "true",
            "remote_release_active": self._is_remote_release_active(settings),
            "timezone": schedule_timezone,
            "devices_total": counts["devices_total"],
            "devices_connected": counts["devices_connected"],
            "judge_ok": settings.get("judge_ok", "true") == "true",
            "last_error": settings.get("last_error", ""),
            "mqtt_enabled": settings.get("mqtt_enabled", "false") == "true",
            "mqtt_connected": self.mqtt.info().get("connected", False),
            "mqtt_last_error": self.mqtt.info().get("last_error", ""),
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

    async def sponsorblock_enabled_now(self, settings_map: dict[str, str] | None = None) -> bool:
        settings = settings_map or await self.db.all_settings()
        active = settings.get("sponsorblock_active", "false") == "true"
        if not active:
            return False
        schedule_enabled = settings.get("sponsorblock_schedule_enabled", "false") == "true"
        schedule_start = settings.get("sponsorblock_schedule_start", "00:00")
        schedule_end = settings.get("sponsorblock_schedule_end", "23:59")
        timezone_name = settings.get("sponsorblock_timezone", settings.get("timezone", "UTC"))
        schedule_active = ScheduleService.is_active(
            enabled=schedule_enabled,
            start=schedule_start,
            end=schedule_end,
            timezone_name=timezone_name,
        )
        return schedule_active

    @staticmethod
    def _is_remote_release_active(settings_map: dict[str, str]) -> bool:
        raw = (settings_map.get("sponsorblock_release_until") or "").strip()
        if not raw:
            return False
        try:
            until = datetime.fromisoformat(raw)
        except Exception:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > datetime.now(timezone.utc)

    async def workers_should_run(self) -> bool:
        settings = await self.db.all_settings()
        return (await self.monitoring_enabled_now(settings)) or (await self.sponsorblock_enabled_now(settings))

    async def sync_workers(self) -> None:
        enabled = await self.workers_should_run()
        if enabled and not self.workers_enabled:
            await self.lounge.start_for_existing_devices()
            self.workers_enabled = True
            await self.emit_live({"event": "monitoring_state", "active": True})
        elif not enabled and self.workers_enabled:
            await self.lounge.pause_all()
            self.workers_enabled = False
            await self.emit_live({"event": "monitoring_state", "active": False})

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

    async def _cancel_reinforce_tasks(self) -> None:
        if not self.reinforce_tasks:
            return
        running = list(self.reinforce_tasks.values())
        self.reinforce_tasks.clear()
        for task in running:
            if not task.done():
                task.cancel()
        await asyncio.gather(*running, return_exceptions=True)

    async def set_monitoring_active(self, active: bool) -> None:
        await self._set_bool_setting_confirmed("active", active)
        if not active:
            await self._cancel_reinforce_tasks()
            self.block_retry_at.clear()
            self.up_next_candidates.clear()
        await self.db.set_setting("last_error", "")
        await self.sync_workers()
        await self.publish_mqtt_snapshot()

    async def set_sponsorblock_active(self, active: bool) -> None:
        await self._set_bool_setting_confirmed("sponsorblock_active", active)
        await self.sync_workers()
        await self.publish_mqtt_snapshot()

    async def set_remote_release_minutes(self, minutes: int) -> str:
        until = ""
        safe_minutes = max(0, min(240, int(minutes)))
        if safe_minutes > 0:
            until = (datetime.now(timezone.utc) + timedelta(minutes=safe_minutes)).isoformat()
        await self.db.set_setting("sponsorblock_release_until", until)
        await self.publish_mqtt_snapshot()
        return until

    @staticmethod
    def _parse_mqtt_bool_payload(raw: str) -> bool | None:
        value = (raw or "").strip().lower()
        if value in {"1", "on", "true", "yes"}:
            return True
        if value in {"0", "off", "false", "no"}:
            return False
        return None

    @staticmethod
    def _remote_release_minutes_remaining(raw: str) -> int:
        value = (raw or "").strip()
        if not value:
            return 0
        try:
            until = datetime.fromisoformat(value)
        except Exception:
            return 0
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        remaining = (until - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return 0
        return max(1, int(remaining // 60))

    async def publish_mqtt_snapshot(
        self,
        *,
        force_discovery: bool = False,
        settings_map: dict[str, str] | None = None,
    ) -> None:
        settings = settings_map or await self.db.all_settings()
        await self.mqtt.apply_settings(settings)
        info = self.mqtt.info()
        if not info.get("enabled", False):
            return

        await self.mqtt.publish_discovery(build_version=self.settings.build_version, force=force_discovery)
        status = await self.get_status()
        dashboard = await self.db.home_dashboard_stats(days=7)
        db_stats = await self.db.db_stats()
        today = dashboard["trend"][-1] if dashboard.get("trend") else {"allow_count": 0, "block_count": 0, "total": 0}
        totals = dashboard.get("totals", {})
        trend_rows = dashboard.get("trend", [])
        blocked_7d = sum(int(row.get("block_count", 0)) for row in trend_rows)
        allowed_7d = sum(int(row.get("allow_count", 0)) for row in trend_rows)
        reviewed_7d = sum(int(row.get("total", 0)) for row in trend_rows)
        payload = {
            "active": status.get("active", False),
            "monitoring_effective": status.get("monitoring_effective", False),
            "sponsorblock_active": status.get("sponsorblock_configured", False),
            "sponsorblock_effective": status.get("sponsorblock_active", False),
            "judge_ok": status.get("judge_ok", True),
            "schedule_active_now": status.get("schedule_active_now", False),
            "schedule_mode_now": status.get("schedule_mode_now", "blocklist"),
            "schedules_count": status.get("schedules_count", 0),
            "timezone": status.get("timezone", "UTC"),
            "build_version": status.get("build_version", self.settings.build_version),
            "remote_release_active": status.get("remote_release_active", False),
            "devices_connected": status.get("devices_connected", 0),
            "devices_total": status.get("devices_total", 0),
            "blocked_today": today.get("block_count", 0),
            "allowed_today": today.get("allow_count", 0),
            "reviewed_today": today.get("total", 0),
            "blocked_7d": blocked_7d,
            "allowed_7d": allowed_7d,
            "reviewed_7d": reviewed_7d,
            "blocked_total": totals.get("block_count", 0),
            "allowed_total": totals.get("allow_count", 0),
            "db_size_bytes": db_stats.get("total_bytes", 0),
            "remote_release_minutes": self._remote_release_minutes_remaining(settings.get("sponsorblock_release_until", "")),
            "last_error": status.get("last_error", ""),
        }
        await self.mqtt.publish_snapshot(payload)

    async def process_mqtt_commands(self) -> None:
        commands = await self.mqtt.drain_commands()
        if not commands:
            return

        changed = False
        for command, payload in commands:
            if command == "active":
                parsed = self._parse_mqtt_bool_payload(payload)
                if parsed is None:
                    continue
                await self.set_monitoring_active(parsed)
                await self.emit_live(
                    {"event": "mqtt_state_change", "target": "active", "active": parsed, "timestamp": utc_now_iso()}
                )
                changed = True
            elif command == "sponsorblock_active":
                parsed = self._parse_mqtt_bool_payload(payload)
                if parsed is None:
                    continue
                await self.set_sponsorblock_active(parsed)
                await self.emit_live(
                    {
                        "event": "mqtt_state_change",
                        "target": "sponsorblock_active",
                        "active": parsed,
                        "timestamp": utc_now_iso(),
                    }
                )
                changed = True
            elif command == "remote_release_minutes":
                try:
                    minutes = max(0, min(240, int((payload or "0").strip())))
                except Exception:
                    continue
                until = await self.set_remote_release_minutes(minutes)
                await self.emit_live(
                    {
                        "event": "mqtt_state_change",
                        "target": "remote_release_minutes",
                        "minutes": minutes,
                        "until": until,
                        "timestamp": utc_now_iso(),
                    }
                )
                changed = True

        if changed:
            await self.publish_mqtt_snapshot(force_discovery=False)

    async def tick_mqtt(self) -> None:
        settings_map = await self.db.all_settings()
        await self.mqtt.apply_settings(settings_map)
        await self.process_mqtt_commands()
        if not self.mqtt.info().get("enabled", False):
            return
        now_mono = monotonic()
        if now_mono < self.mqtt_publish_due_at:
            return
        await self.publish_mqtt_snapshot(force_discovery=False, settings_map=settings_map)
        self.mqtt_publish_due_at = now_mono + self.mqtt.publish_interval_seconds

    @staticmethod
    def _parse_sponsorblock_categories(raw: str) -> list[str]:
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, list):
                out = [str(x).strip() for x in loaded if str(x).strip()]
                if out:
                    return out
        except Exception:
            pass
        return list(DEFAULT_SPONSORBLOCK_CATEGORIES)

    @staticmethod
    def _parse_float_setting(raw: str, default: float) -> float:
        try:
            return float(raw)
        except Exception:
            return default

    def _remember_up_next_candidate(self, device_id: int, video_id: str) -> None:
        if not video_id:
            return
        q = self.up_next_candidates.setdefault(device_id, [])
        q = [v for v in q if v != video_id]
        q.append(video_id)
        if len(q) > 30:
            q = q[-30:]
        self.up_next_candidates[device_id] = q

    def _drop_candidate(self, device_id: int, video_id: str) -> None:
        if not video_id:
            return
        q = self.up_next_candidates.get(device_id, [])
        if not q:
            return
        self.up_next_candidates[device_id] = [v for v in q if v != video_id]

    async def _play_safe_from_queue(
        self,
        *,
        device_id: int,
        blocked_video_id: str,
        enforcement_mode: str,
    ) -> tuple[bool, str, str]:
        queue = [v for v in self.up_next_candidates.get(device_id, []) if v and v != blocked_video_id]
        if not queue:
            return await self._play_safe_from_history(
                device_id=device_id,
                blocked_video_id=blocked_video_id,
                enforcement_mode=enforcement_mode,
            )

        last_error = ""
        for candidate_id in queue[:12]:
            meta = await fetch_video_metadata(candidate_id)
            video_url = f"https://www.youtube.com/watch?v={candidate_id}"
            try:
                candidate_decision = await self.judge.evaluate(
                    video_id=candidate_id,
                    title=meta.get("title", ""),
                    channel_id=meta.get("channel_id", ""),
                    channel_title=meta.get("channel_title", ""),
                    video_url=video_url,
                    enforcement_mode=enforcement_mode,
                )
            except GeminiFatalError as err:
                await self.judge.handle_fatal_failure(err)
                if enforcement_mode == "whitelist":
                    candidate_decision = {
                        "verdict": "BLOCK",
                        "reason": "Whitelist mode: Gemini unavailable for candidate evaluation.",
                        "confidence": 100,
                        "source": "fallback",
                    }
                else:
                    candidate_decision = {
                        "verdict": "ALLOW",
                        "reason": "Gemini unavailable; fail-open candidate allow.",
                        "confidence": 0,
                        "source": "fallback",
                    }
            except Exception:
                if enforcement_mode == "whitelist":
                    candidate_decision = {
                        "verdict": "BLOCK",
                        "reason": "Whitelist mode: candidate evaluation failed.",
                        "confidence": 100,
                        "source": "fallback",
                    }
                else:
                    candidate_decision = {
                        "verdict": "ALLOW",
                        "reason": "Candidate evaluation failed; fail-open candidate allow.",
                        "confidence": 0,
                        "source": "fallback",
                    }

            if candidate_decision.get("verdict") != "ALLOW":
                continue

            ok, err = await self.lounge.play_video(device_id, candidate_id)
            if ok:
                self._drop_candidate(device_id, candidate_id)
                return True, "", candidate_id
            last_error = err or "TV refused to play safe candidate video."

        # Fallback to known-safe history entry if queue candidates are all blocked/failed.
        hist_ok, hist_err, hist_id = await self._play_safe_from_history(
            device_id=device_id,
            blocked_video_id=blocked_video_id,
            enforcement_mode=enforcement_mode,
        )
        if hist_ok:
            return True, "", hist_id
        if last_error:
            return False, f"{last_error} {hist_err}".strip(), ""
        return False, hist_err or "No safe video found in queued candidates.", ""

    @staticmethod
    def _history_allow_candidates(rows: list[dict[str, Any]], blocked_video_id: str) -> list[str]:
        candidate_ids: list[str] = []
        seen: set[str] = set()
        for row in rows:
            if row.get("verdict") != "ALLOW":
                continue
            candidate_id = (row.get("video_id") or "").strip()
            if not candidate_id or candidate_id == blocked_video_id or candidate_id in seen:
                continue
            seen.add(candidate_id)
            candidate_ids.append(candidate_id)
        return candidate_ids

    def _randomized_history_candidates(self, *, device_id: int, candidate_ids: list[str]) -> list[str]:
        if not candidate_ids:
            return []
        randomized = list(candidate_ids)
        random.shuffle(randomized)
        last_choice = self.last_history_choice.get(device_id, "")
        if last_choice and len(randomized) > 1 and randomized[0] == last_choice:
            for idx, candidate_id in enumerate(randomized):
                if candidate_id != last_choice:
                    randomized[0], randomized[idx] = randomized[idx], randomized[0]
                    break
        return randomized

    async def _play_safe_from_history(
        self,
        *,
        device_id: int,
        blocked_video_id: str,
        enforcement_mode: str,
    ) -> tuple[bool, str, str]:
        rows = await self.db.recent_video_decisions(limit=500)
        candidate_ids = self._randomized_history_candidates(
            device_id=device_id,
            candidate_ids=self._history_allow_candidates(rows, blocked_video_id),
        )
        if not candidate_ids:
            return False, "No known-safe history video available for fallback.", ""

        last_error = ""
        for candidate_id in candidate_ids:
            meta = await fetch_video_metadata(candidate_id)
            video_url = f"https://www.youtube.com/watch?v={candidate_id}"
            try:
                candidate_decision = await self.judge.evaluate(
                    video_id=candidate_id,
                    title=meta.get("title", ""),
                    channel_id=meta.get("channel_id", ""),
                    channel_title=meta.get("channel_title", ""),
                    video_url=video_url,
                    enforcement_mode=enforcement_mode,
                )
            except GeminiFatalError as err:
                await self.judge.handle_fatal_failure(err)
                if enforcement_mode == "whitelist":
                    candidate_decision = {
                        "verdict": "BLOCK",
                        "reason": "Whitelist mode: Gemini unavailable for history candidate.",
                        "confidence": 100,
                        "source": "fallback",
                    }
                else:
                    candidate_decision = {
                        "verdict": "ALLOW",
                        "reason": "Gemini unavailable; fail-open history candidate allow.",
                        "confidence": 0,
                        "source": "fallback",
                    }
            except Exception:
                if enforcement_mode == "whitelist":
                    candidate_decision = {
                        "verdict": "BLOCK",
                        "reason": "Whitelist mode: history candidate evaluation failed.",
                        "confidence": 100,
                        "source": "fallback",
                    }
                else:
                    candidate_decision = {
                        "verdict": "ALLOW",
                        "reason": "History candidate evaluation failed; fail-open allow.",
                        "confidence": 0,
                        "source": "fallback",
                    }

            if candidate_decision.get("verdict") != "ALLOW":
                continue

            ok, err = await self.lounge.play_video(device_id, candidate_id)
            if ok:
                self.last_history_choice[device_id] = candidate_id
                return True, "", candidate_id
            last_error = err or "TV refused to play known-safe history video."

        if last_error:
            return False, last_error, ""
        return False, "No known-safe history video available for fallback.", ""

    async def _reinforce_safe_play(self, *, device_id: int, safe_video_id: str) -> None:
        # Some TV clients ignore the first override while user-initiated playback is still settling.
        for delay in (1.0, 3.0):
            await asyncio.sleep(delay)
            settings_map = await self.db.all_settings()
            if not await self.monitoring_enabled_now(settings_map):
                return
            if self._is_remote_release_active(settings_map):
                return
            ok, _err = await self.lounge.play_video(device_id, safe_video_id)
            if ok:
                await self.emit_live(
                    {
                        "event": "intervention_play_safe_reinforce",
                        "device_id": device_id,
                        "safe_video_id": safe_video_id,
                        "timestamp": utc_now_iso(),
                    }
                )

    async def process_sponsorblock_event(self, event: dict[str, Any]) -> None:
        et = event.get("event", "")
        if et not in {"now_playing", "up_next"}:
            return
        settings_map = await self.db.all_settings()
        if not await self.sponsorblock_enabled_now(settings_map):
            return
        if self._is_remote_release_active(settings_map):
            return

        device_id = int(event["device_id"])
        video_id = str(event.get("video_id", "")).strip()
        if not video_id:
            return
        categories = self._parse_sponsorblock_categories(settings_map.get("sponsorblock_categories_json", "[]"))
        min_len = self._parse_float_setting(settings_map.get("sponsorblock_min_length_seconds", "1.0"), 1.0)
        if et == "up_next":
            await self.sponsorblock.prefetch(video_id=video_id, categories=categories, min_length=min_len)
            return

        play_state = event.get("play_state")
        if play_state is not None and str(play_state) != "1":
            return
        current_time_raw = event.get("current_time")
        try:
            current_time = float(current_time_raw) if current_time_raw is not None else None
        except Exception:
            current_time = None
        ok, err, segment = await self.sponsorblock.try_skip_current(
            device_id=device_id,
            video_id=video_id,
            current_time=current_time,
            categories=categories,
            min_length=min_len,
            lounge_seek=self.lounge.seek_video,
        )
        if not segment:
            return
        meta = await fetch_video_metadata(video_id)
        action = "seek_end" if ok else "none"
        await self.db.add_sponsorblock_action(
            device_id=device_id,
            video_id=video_id,
            title=meta.get("title", ""),
            category=str(segment.get("category", "")),
            segment_start=float(segment.get("start", 0.0)),
            segment_end=float(segment.get("end", 0.0)),
            action_taken=action,
            status="ok" if ok else "error",
            error=err or "",
        )
        if ok:
            await self.emit_live(
                {
                    "event": "sponsorblock_skip",
                    "device_id": device_id,
                    "video_id": video_id,
                    "title": meta.get("title", ""),
                    "segment_start": segment.get("start"),
                    "segment_end": segment.get("end"),
                    "category": segment.get("category", ""),
                    "action_taken": action,
                    "timestamp": utc_now_iso(),
                }
            )
        elif err:
            await self.emit_live(
                {
                    "event": "sponsorblock_error",
                    "device_id": device_id,
                    "video_id": video_id,
                    "message": err,
                    "timestamp": utc_now_iso(),
                }
            )

    async def process_lounge_event(self, event: dict[str, Any]) -> None:
        et = event.get("event", "")
        if et == "device_status":
            await self.emit_live(event)
            return

        if et not in {"now_playing", "up_next"}:
            return

        await self.process_sponsorblock_event(event)

        settings_map = await self.db.all_settings()
        monitoring = await self.monitoring_enabled_now(settings_map)
        if not monitoring:
            return

        device_id = int(event["device_id"])
        video_id = str(event.get("video_id", "")).strip()
        if not video_id:
            return
        if et == "up_next":
            self._remember_up_next_candidate(device_id, video_id)

        inferred_now_playing = False
        now_mono = monotonic()
        if et == "now_playing":
            prev_now = self.last_now_playing_video.get(device_id)
            if prev_now and prev_now[0] == video_id and (now_mono - prev_now[1]) < 5.0:
                return
            self.last_now_playing_video[device_id] = (video_id, now_mono)
            self.last_now_playing_at[device_id] = now_mono
            self.up_next_repeat.pop(device_id, None)
            self._drop_candidate(device_id, video_id)
        else:
            prev_video, prev_count = self.up_next_repeat.get(device_id, ("", 0))
            if prev_video == video_id:
                prev_count += 1
            else:
                prev_count = 1
            self.up_next_repeat[device_id] = (video_id, prev_count)
            recent_now_playing = (now_mono - self.last_now_playing_at.get(device_id, 0.0)) < 4.0
            inferred_now_playing = (not recent_now_playing) and prev_count >= 2

        meta = await fetch_video_metadata(video_id)
        video_url = f"https://www.youtube.com/watch?v={video_id}"
        schedule_ctx = await self.current_schedule_context(settings_map=settings_map)
        enforcement_mode = str(schedule_ctx.get("mode", "blocklist"))

        try:
            decision = await self.judge.evaluate(
                video_id=video_id,
                title=meta.get("title", ""),
                channel_id=meta.get("channel_id", ""),
                channel_title=meta.get("channel_title", ""),
                video_url=video_url,
                enforcement_mode=enforcement_mode,
            )
            await self.db.set_setting("judge_ok", "true")
            await self.db.set_setting("last_error", "")
        except GeminiFatalError as err:
            await self.judge.handle_fatal_failure(err)
            if enforcement_mode == "whitelist":
                decision = {
                    "verdict": "BLOCK",
                    "reason": "Whitelist mode: Gemini unavailable and no explicit allowlist match.",
                    "confidence": 100,
                    "source": "fallback",
                }
            else:
                decision = {
                    "verdict": "ALLOW",
                    "reason": "Gemini is temporarily unavailable (quota/auth). Allowed by fail-open policy.",
                    "confidence": 0,
                    "source": "fallback",
                }
            await self.emit_live(
                {
                    "event": "judge_failure",
                    "error": str(err),
                    "active": True,
                    "timestamp": utc_now_iso(),
                }
            )
        except Exception as err:
            if enforcement_mode == "whitelist":
                decision = {
                    "verdict": "BLOCK",
                    "reason": f"Whitelist mode fallback block due to parser/runtime error: {err}",
                    "confidence": 100,
                    "source": "fallback",
                }
            else:
                decision = {
                    "verdict": "ALLOW",
                    "reason": f"Fallback allow due to parser/runtime error: {err}",
                    "confidence": 0,
                    "source": "fallback",
                }

        action = "none"
        should_treat_as_current = (
            et == "now_playing" or inferred_now_playing or (et == "up_next" and decision["verdict"] == "BLOCK")
        )
        release_active = self._is_remote_release_active(settings_map)
        if should_treat_as_current and decision["verdict"] == "BLOCK":
            if release_active:
                action = "none"
            else:
                retry_key = f"{device_id}:{video_id}"
                now_mono = monotonic()
                last_try = self.block_retry_at.get(retry_key, 0.0)
                if now_mono - last_try < 1.5:
                    action = "none"
                else:
                    self.block_retry_at[retry_key] = now_mono
                    ok, skip_error, safe_video_id = await self._play_safe_from_queue(
                        device_id=device_id,
                        blocked_video_id=video_id,
                        enforcement_mode=enforcement_mode,
                    )
                    action = "play_safe" if ok else "none"
                    if ok:
                        old_task = self.reinforce_tasks.get(device_id)
                        if old_task and not old_task.done():
                            old_task.cancel()
                        self.reinforce_tasks[device_id] = asyncio.create_task(
                            self._reinforce_safe_play(device_id=device_id, safe_video_id=safe_video_id),
                            name=f"reinforce-safe-{device_id}",
                        )
                        # Clear stale retry markers for this device once skip succeeded.
                        prefix = f"{device_id}:"
                        for key in [k for k in self.block_retry_at.keys() if k.startswith(prefix)]:
                            self.block_retry_at.pop(key, None)
                        await self.emit_live(
                            {
                                "event": "intervention_play_safe",
                                "device_id": device_id,
                                "blocked_video_id": video_id,
                                "safe_video_id": safe_video_id,
                                "timestamp": utc_now_iso(),
                            }
                        )
                    elif skip_error:
                        await self.emit_live(
                            {
                                "event": "intervention_error",
                                "device_id": device_id,
                                "video_id": video_id,
                                "message": skip_error,
                                "timestamp": utc_now_iso(),
                            }
                        )
        elif should_treat_as_current:
            action = "allow"

        await self.db.add_video_decision(
            device_id=device_id,
            video_id=video_id,
            channel_id=meta.get("channel_id", ""),
            title=meta.get("title", ""),
            thumbnail_url=meta.get("thumbnail_url", ""),
            verdict=decision["verdict"],
            reason=decision["reason"],
            confidence=int(decision["confidence"]),
            source=decision["source"],
            action_taken=action,
        )

        await self.emit_live(
            {
                "event": et,
                "device_id": device_id,
                "video_id": video_id,
                "title": meta.get("title", ""),
                "channel_title": meta.get("channel_title", ""),
                "thumbnail_url": meta.get("thumbnail_url", ""),
                "verdict": decision["verdict"],
                "reason": decision["reason"],
                "confidence": decision["confidence"],
                "source": decision["source"],
                "action_taken": action,
                "inferred_now_playing": inferred_now_playing,
                "timestamp": utc_now_iso(),
            }
        )

    async def supervisor(self) -> None:
        while True:
            try:
                await self.sync_workers()
                await self.tick_mqtt()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(5)


async def fetch_video_metadata(video_id: str) -> dict[str, str]:
    url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    timeout = aiohttp.ClientTimeout(total=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return {
                        "title": f"Video {video_id}",
                        "channel_title": "",
                        "channel_id": "",
                        "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                    }
                data = await resp.json()
                return {
                    "title": data.get("title", f"Video {video_id}"),
                    "channel_title": data.get("author_name", ""),
                    "channel_id": "",
                    "thumbnail_url": data.get("thumbnail_url", f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"),
                }
    except Exception:
        return {
            "title": f"Video {video_id}",
            "channel_title": "",
            "channel_id": "",
            "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        }


settings = Settings()
db = Database(settings.db_path)
webhook_client = WebhookClient(settings.webhook_timeout_seconds)
discovery = DiscoveryService()
blocklists = BlocklistService(settings)
allowlists = BlocklistService(settings, list_kind="whitelist")
sponsorblock = SponsorBlockService(settings)
mqtt_bridge = MQTTBridge(settings)
judge = JudgeService(db, settings, webhook_client, blocklists=blocklists, allowlists=allowlists)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    await db.init()
    await blocklists.reload(db)
    await allowlists.reload(db)
    mqtt_bridge.set_event_loop(asyncio.get_running_loop())
    runtime = RuntimeState(
        settings=settings,
        db=db,
        discovery=discovery,
        webhook_client=webhook_client,
        judge=judge,
        lounge=LoungeManager(db=db, settings=settings, event_callback=lambda e: app.state.runtime.process_lounge_event(e)),
        blocklists=blocklists,
        allowlists=allowlists,
        sponsorblock=sponsorblock,
        mqtt=mqtt_bridge,
    )
    app.state.runtime = runtime
    await runtime.publish_mqtt_snapshot(force_discovery=True)
    runtime.supervisor_task = asyncio.create_task(runtime.supervisor(), name="sentinel-supervisor")
    yield
    if runtime.supervisor_task:
        runtime.supervisor_task.cancel()
        await asyncio.gather(runtime.supervisor_task, return_exceptions=True)
    if runtime.reinforce_tasks:
        for task in runtime.reinforce_tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*runtime.reinforce_tasks.values(), return_exceptions=True)
    await runtime.mqtt.close()
    await runtime.lounge.stop_all()


app = FastAPI(title="Sentinel", lifespan=lifespan)
base_dir = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(base_dir / "templates"))
app.mount("/static", StaticFiles(directory=str(base_dir / "static")), name="static")


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
    runtime: RuntimeState = request.app.state.runtime
    status = await runtime.get_status()
    dashboard = await runtime.db.home_dashboard_stats(days=7)
    db_stats = await runtime.db.db_stats()
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "status": status,
            "dashboard": dashboard,
            "db_stats": db_stats,
            "page": "home",
        },
    )


@app.get("/live", response_class=HTMLResponse)
async def page_live(request: Request) -> HTMLResponse:
    status = await request.app.state.runtime.get_status()
    decisions = await request.app.state.runtime.db.recent_video_decisions(limit=20)
    devices = await request.app.state.runtime.db.list_devices()
    return templates.TemplateResponse(
        request,
        "live.html",
        {"status": status, "decisions": decisions, "devices": devices, "page": "live"},
    )


@app.get("/history", response_class=HTMLResponse)
async def page_history(request: Request, page: int = 1) -> HTMLResponse:
    paged = await request.app.state.runtime.db.paged_video_decisions(page=page, page_size=50, max_total=500)
    status = await request.app.state.runtime.get_status()
    return templates.TemplateResponse(
        request,
        "history.html",
        {"rows": paged["rows"], "pager": paged, "status": status, "page": "history"},
    )


@app.get("/rules")
async def page_rules_alias() -> RedirectResponse:
    return RedirectResponse(url="/blocklist", status_code=307)


@app.get("/blocklist", response_class=HTMLResponse)
async def page_blocklist(request: Request) -> HTMLResponse:
    runtime: RuntimeState = request.app.state.runtime
    rules = await runtime.db.list_rules(limit=200, rule_type="blacklist")
    settings_map = await runtime.db.all_settings()
    policy_flags = normalize_policy_flags(settings_map.get("policy_flags_json", "{}"))
    status = await runtime.get_status()
    blocked_recent = await runtime.db.recent_blocked_decisions(limit=10)
    blocklist_summary = runtime.blocklists.summary()
    sources = await runtime.blocklists.get_sources(runtime.db)
    local_blocklist = await runtime.blocklists.get_local_content()
    return templates.TemplateResponse(
        request,
        "blocklist.html",
        {
            "rules": rules,
            "blocked_recent": blocked_recent,
            "blocklist_summary": blocklist_summary,
            "blocklist_sources": sources,
            "local_blocklist_content": local_blocklist,
            "status": status,
            "policy_presets": POLICY_PRESETS,
            "policy_flags": policy_flags,
            "page": "blocklist",
        },
    )


@app.get("/allowlist", response_class=HTMLResponse)
async def page_allowlist(request: Request) -> HTMLResponse:
    runtime: RuntimeState = request.app.state.runtime
    rules = await runtime.db.list_rules(limit=200, rule_type="whitelist")
    settings_map = await runtime.db.all_settings()
    allow_policy_flags = normalize_allow_policy_flags(settings_map.get("allow_policy_flags_json", "{}"))
    status = await runtime.get_status()
    allowed_recent = await runtime.db.recent_allowed_decisions(limit=10)
    allowlist_summary = runtime.allowlists.summary()
    sources = await runtime.allowlists.get_sources(runtime.db)
    local_allowlist = await runtime.allowlists.get_local_content()
    effective_prompt = await runtime.judge.get_effective_whitelist_prompt_preview()
    return templates.TemplateResponse(
        request,
        "allowlist.html",
        {
            "rules": rules,
            "allowed_recent": allowed_recent,
            "allowlist_summary": allowlist_summary,
            "allowlist_sources": sources,
            "local_allowlist_content": local_allowlist,
            "status": status,
            "allow_policy_presets": ALLOW_POLICY_PRESETS,
            "allow_policy_flags": allow_policy_flags,
            "effective_whitelist_prompt": effective_prompt,
            "page": "allowlist",
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


@app.get("/devices", response_class=HTMLResponse)
async def page_devices(request: Request) -> HTMLResponse:
    devices = await request.app.state.runtime.db.list_devices()
    status = await request.app.state.runtime.get_status()
    discovered = request.app.state.runtime.discovered_devices
    return templates.TemplateResponse(
        request,
        "devices.html",
        {"devices": devices, "discovered": discovered, "status": status, "page": "devices"},
    )


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request) -> HTMLResponse:
    status = await request.app.state.runtime.get_status()
    settings_map = await request.app.state.runtime.db.all_settings()
    custom_prompt = (settings_map.get("custom_prompt") or "").strip()
    base_prompt = custom_prompt or DEFAULT_SAFE_PROMPT
    effective_prompt = await request.app.state.runtime.judge.get_effective_prompt_preview()
    db_stats = await request.app.state.runtime.db.db_stats()
    gemini_enabled = (settings_map.get("gemini_enabled", "true") == "true")
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "status": status,
            "settings": settings_map,
            "gemini_enabled": gemini_enabled,
            "base_prompt": base_prompt,
            "is_using_default_prompt": not bool(custom_prompt),
            "effective_prompt": effective_prompt,
            "output_contract": OUTPUT_CONTRACT_SUFFIX,
            "timezones": SUPPORTED_TIMEZONES,
            "db_stats": db_stats,
            "page": "settings",
        },
    )


@app.get("/automation", response_class=HTMLResponse)
async def page_automation(request: Request) -> HTMLResponse:
    status = await request.app.state.runtime.get_status()
    return templates.TemplateResponse(request, "automation.html", {"status": status, "page": "automation"})


@app.get("/mqtt", response_class=HTMLResponse)
async def page_mqtt(request: Request) -> HTMLResponse:
    runtime: RuntimeState = request.app.state.runtime
    status = await runtime.get_status()
    settings_map = await runtime.db.all_settings()
    mqtt_info = runtime.mqtt.info()
    return templates.TemplateResponse(
        request,
        "mqtt.html",
        {
            "status": status,
            "settings": settings_map,
            "mqtt": mqtt_info,
            "page": "mqtt",
        },
    )


@app.get("/sponsorblock", response_class=HTMLResponse)
async def page_sponsorblock(request: Request) -> HTMLResponse:
    runtime: RuntimeState = request.app.state.runtime
    status = await runtime.get_status()
    settings_map = await runtime.db.all_settings()
    actions = await runtime.db.recent_sponsorblock_actions(limit=100)
    categories = RuntimeState._parse_sponsorblock_categories(settings_map.get("sponsorblock_categories_json", "[]"))
    return templates.TemplateResponse(
        request,
        "sponsorblock.html",
        {
            "status": status,
            "settings": settings_map,
            "actions": actions,
            "categories": categories,
            "timezones": SUPPORTED_TIMEZONES,
            "page": "sponsorblock",
        },
    )


@app.post("/api/control/state")
async def api_control_state(payload: ControlStateRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.set_monitoring_active(payload.active)
    status = await runtime.get_status()
    await runtime.emit_live({"event": "manual_state_change", "active": payload.active, "timestamp": utc_now_iso()})
    return {
        "active": status["active"],
        "monitoring_effective": status["monitoring_effective"],
        "changed_at": utc_now_iso(),
        "reason": "manual",
    }


@app.get("/api/status")
async def api_status(request: Request) -> dict[str, Any]:
    return await request.app.state.runtime.get_status()


@app.post("/api/webhook/control")
async def api_webhook_control(payload: WebhookControlRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.set_monitoring_active(payload.active)
    status = await runtime.get_status()
    await runtime.emit_live(
        {
            "event": "webhook_state_change",
            "active": payload.active,
            "source": payload.source,
            "timestamp": utc_now_iso(),
        }
    )
    return {
        "ok": True,
        "active": status["active"],
        "monitoring_effective": status["monitoring_effective"],
        "source": payload.source,
    }


@app.post("/api/sponsorblock/state")
async def api_sponsorblock_state(payload: SponsorBlockStateRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.set_sponsorblock_active(payload.active)
    status = await runtime.get_status()
    await runtime.emit_live(
        {"event": "sponsorblock_state_change", "active": payload.active, "source": "dashboard", "timestamp": utc_now_iso()}
    )
    return {
        "ok": True,
        "active": status["sponsorblock_configured"],
        "effective_active": status["sponsorblock_active"],
    }


@app.post("/api/webhook/sponsorblock/state")
async def api_webhook_sponsorblock_state(payload: WebhookControlRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.set_sponsorblock_active(payload.active)
    status = await runtime.get_status()
    await runtime.emit_live(
        {"event": "sponsorblock_state_change", "active": payload.active, "source": payload.source, "timestamp": utc_now_iso()}
    )
    return {
        "ok": True,
        "active": status["sponsorblock_configured"],
        "effective_active": status["sponsorblock_active"],
        "source": payload.source,
    }


@app.post("/api/sponsorblock/schedule")
async def api_sponsorblock_schedule(payload: SponsorBlockScheduleRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.db.set_setting("sponsorblock_schedule_enabled", "true" if payload.enabled else "false")
    await runtime.db.set_setting("sponsorblock_schedule_start", payload.start)
    await runtime.db.set_setting("sponsorblock_schedule_end", payload.end)
    await runtime.db.set_setting("sponsorblock_timezone", payload.timezone)
    await runtime.sync_workers()
    return {"ok": True}


@app.post("/api/sponsorblock/config")
async def api_sponsorblock_config(payload: SponsorBlockConfigRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    categories = payload.categories or list(DEFAULT_SPONSORBLOCK_CATEGORIES)
    await runtime.db.set_setting("sponsorblock_categories_json", json.dumps(categories))
    await runtime.db.set_setting("sponsorblock_min_length_seconds", str(payload.min_length_seconds))
    return {"ok": True, "categories": categories, "min_length_seconds": payload.min_length_seconds}


@app.post("/api/sponsorblock/release")
async def api_sponsorblock_release(payload: SponsorBlockReleaseRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    until = await runtime.set_remote_release_minutes(payload.minutes)
    await runtime.emit_live(
        {
            "event": "remote_release_change",
            "active": bool(until),
            "until": until,
            "minutes": payload.minutes,
            "source": payload.source or "dashboard",
            "reason": payload.reason,
            "timestamp": utc_now_iso(),
        }
    )
    return {"ok": True, "active": bool(until), "until": until, "minutes": payload.minutes}


@app.post("/api/webhook/sponsorblock/release")
async def api_webhook_sponsorblock_release(payload: SponsorBlockReleaseRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    until = await runtime.set_remote_release_minutes(payload.minutes)
    await runtime.emit_live(
        {
            "event": "remote_release_change",
            "active": bool(until),
            "until": until,
            "minutes": payload.minutes,
            "source": payload.source or "home_assistant",
            "reason": payload.reason,
            "timestamp": utc_now_iso(),
        }
    )
    return {"ok": True, "active": bool(until), "until": until, "minutes": payload.minutes}


@app.post("/api/mqtt/state")
async def api_mqtt_state(payload: MqttStateRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime._set_bool_setting_confirmed("mqtt_enabled", payload.enabled)
    await runtime.publish_mqtt_snapshot(force_discovery=True)
    await runtime.emit_live(
        {"event": "mqtt_state_change", "target": "mqtt_enabled", "active": payload.enabled, "timestamp": utc_now_iso()}
    )
    return {"ok": True, "enabled": runtime.mqtt.info().get("enabled", False), "mqtt": runtime.mqtt.info()}


@app.post("/api/mqtt/config")
async def api_mqtt_config(payload: MqttConfigRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.db.set_setting("mqtt_enabled", "true" if payload.enabled else "false")
    await runtime.db.set_setting("mqtt_host", payload.host.strip())
    await runtime.db.set_setting("mqtt_port", str(payload.port))
    await runtime.db.set_setting("mqtt_username", payload.username.strip())
    await runtime.db.set_setting("mqtt_password", payload.password)
    await runtime.db.set_setting("mqtt_base_topic", payload.base_topic.strip())
    await runtime.db.set_setting("mqtt_discovery_prefix", payload.discovery_prefix.strip())
    await runtime.db.set_setting("mqtt_retain", "true" if payload.retain else "false")
    await runtime.db.set_setting("mqtt_tls", "true" if payload.tls else "false")
    await runtime.db.set_setting("mqtt_publish_interval_seconds", str(payload.publish_interval_seconds))
    await runtime.publish_mqtt_snapshot(force_discovery=True)
    await runtime.emit_live({"event": "mqtt_config_saved", "timestamp": utc_now_iso()})
    return {"ok": True, "mqtt": runtime.mqtt.info()}


@app.post("/api/mqtt/publish")
async def api_mqtt_publish(request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.publish_mqtt_snapshot(force_discovery=True)
    return {"ok": True, "mqtt": runtime.mqtt.info()}


@app.post("/api/devices/scan")
async def api_devices_scan(request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    runtime.discovered_devices = await runtime.discovery.scan()
    count = len(runtime.discovered_devices)
    await runtime.emit_live({"event": "scan_result", "count": count})
    return {"devices": runtime.discovered_devices, "count": count, "scanned_at": utc_now_iso()}


@app.post("/api/devices/pair")
async def api_devices_pair(payload: PairDeviceRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    selected_ref = payload.device_ref.strip()
    selected_code = payload.pairing_code.strip()
    chosen = next((d for d in runtime.discovered_devices if d.get("device_ref") == selected_ref), None)
    if not chosen:
        logger.warning("pair failed: code=pair_device_not_found device_ref=%s", selected_ref)
        raise HTTPException(
            status_code=404,
            detail={
                "code": "pair_device_not_found",
                "message": "The selected device is no longer in the scan list. Scan again and retry pairing.",
            },
        )
    try:
        result = await runtime.lounge.pair_device(selected_code, selected_ref)
    except PairingError as err:
        logger.warning(
            "pair failed: code=%s device_ref=%s message=%s",
            err.code,
            selected_ref,
            err.message,
        )
        raise HTTPException(
            status_code=400,
            detail={"code": err.code, "message": err.message},
        ) from err
    except Exception as err:
        logger.exception("pair failed unexpectedly: device_ref=%s", selected_ref)
        raise HTTPException(
            status_code=400,
            detail={
                "code": "pair_unknown_error",
                "message": "Pairing failed unexpectedly. Please request a new TV code and try again.",
            },
        ) from err
    if chosen and chosen.get("screen_id") and result.get("screen_id") != chosen.get("screen_id"):
        selected_name = chosen.get("display_name") or chosen.get("host") or "selected device"
        paired_name = result.get("name") or "another TV"
        logger.warning(
            "pair failed: code=pair_mismatch device_ref=%s selected=%s paired=%s",
            selected_ref,
            selected_name,
            paired_name,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "pair_mismatch",
                "message": (
                    f'This code was accepted by "{paired_name}", not "{selected_name}". '
                    "Make sure you entered the code from the same TV row and try again."
                ),
            },
        )
    if await runtime.workers_should_run():
        await runtime.lounge.ensure_worker(int(result["device_id"]))
    logger.info(
        "pair success: device_id=%s device_ref=%s screen_id=%s name=%s",
        result.get("device_id"),
        selected_ref,
        result.get("screen_id"),
        result.get("name"),
    )
    await runtime.emit_live({"event": "pair_success", **result, "timestamp": utc_now_iso()})
    await runtime.sync_workers()
    return {"ok": True, **result}


@app.post("/api/devices/pair/code")
async def api_devices_pair_code(payload: PairCodeOnlyRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    try:
        result = await runtime.lounge.pair_device(payload.pairing_code.strip(), "manual-code")
    except PairingError as err:
        logger.warning("pair (manual code) failed: code=%s message=%s", err.code, err.message)
        raise HTTPException(
            status_code=400,
            detail={"code": err.code, "message": err.message},
        ) from err
    except Exception as err:
        logger.exception("pair (manual code) failed unexpectedly")
        raise HTTPException(
            status_code=400,
            detail={
                "code": "pair_unknown_error",
                "message": "Pairing failed unexpectedly. Request a new TV code and try again.",
            },
        ) from err
    if await runtime.workers_should_run():
        await runtime.lounge.ensure_worker(int(result["device_id"]))
    await runtime.emit_live({"event": "pair_success", **result, "timestamp": utc_now_iso()})
    await runtime.sync_workers()
    return {"ok": True, **result, "warning": "Paired by code only. Device scan match was skipped."}


@app.get("/api/live/events")
async def api_live_events(request: Request) -> StreamingResponse:
    runtime: RuntimeState = request.app.state.runtime
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
    runtime.live_subscribers.add(q)

    async def _gen() -> AsyncGenerator[bytes, None]:
        try:
            initial = await runtime.get_status()
            yield f"data: {json.dumps({'event': 'status', **initial})}\n\n".encode("utf-8")
            while True:
                event = await q.get()
                yield f"data: {json.dumps(event)}\n\n".encode("utf-8")
        except asyncio.CancelledError:
            raise
        finally:
            runtime.live_subscribers.discard(q)

    return StreamingResponse(_gen(), media_type="text/event-stream")


@app.post("/api/rules/whitelist")
async def api_whitelist(payload: RuleRequest, request: Request) -> dict[str, Any]:
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
        "whitelist",
        payload.scope,
        value,
        label=label,
        url=url,
        source_list="manual",
    )
    await runtime.allowlists.append_entry(
        scope=payload.scope,
        value=value,
        label=label,
        url=url,
        source_list="manual",
    )
    await runtime.allowlists.reload(runtime.db)
    return {"ok": True}


@app.post("/api/rules/blacklist")
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
    return {"ok": True}


@app.post("/api/blocklist/policies")
@app.post("/api/rules/policies")
async def api_rules_policies(payload: PolicyFlagsRequest, request: Request) -> dict[str, Any]:
    flags = normalize_policy_flags(payload.flags)
    await request.app.state.runtime.db.set_setting("policy_flags_json", json.dumps(flags))
    return {"ok": True, "flags": flags}


@app.post("/api/allowlist/policies")
async def api_allowlist_policies(payload: AllowPolicyFlagsRequest, request: Request) -> dict[str, Any]:
    flags = normalize_allow_policy_flags(payload.flags)
    await request.app.state.runtime.db.set_setting("allow_policy_flags_json", json.dumps(flags))
    return {"ok": True, "flags": flags}


@app.post("/api/blocklist/sources")
@app.post("/api/rules/blocklists/sources")
async def api_rules_blocklists_sources(payload: RulesImportSourcesRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.blocklists.set_sources(runtime.db, payload.urls)
    summary = await runtime.blocklists.reload(runtime.db)
    return {"ok": True, "summary": summary, "sources": payload.urls}


@app.post("/api/blocklist/reload")
@app.post("/api/rules/blocklists/reload")
async def api_rules_blocklists_reload(request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    summary = await runtime.blocklists.reload(runtime.db)
    return {"ok": True, "summary": summary}


@app.post("/api/blocklist/local")
@app.post("/api/rules/blocklists/local")
async def api_rules_blocklists_local(payload: LocalBlocklistContentRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.blocklists.save_local_content(payload.content)
    summary = await runtime.blocklists.reload(runtime.db)
    return {"ok": True, "summary": summary}


@app.post("/api/allowlist/sources")
async def api_allowlist_sources(payload: RulesImportSourcesRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.allowlists.set_sources(runtime.db, payload.urls)
    summary = await runtime.allowlists.reload(runtime.db)
    return {"ok": True, "summary": summary, "sources": payload.urls}


@app.post("/api/allowlist/reload")
async def api_allowlist_reload(request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    summary = await runtime.allowlists.reload(runtime.db)
    return {"ok": True, "summary": summary}


@app.post("/api/allowlist/local")
async def api_allowlist_local(payload: LocalBlocklistContentRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.allowlists.save_local_content(payload.content)
    summary = await runtime.allowlists.reload(runtime.db)
    return {"ok": True, "summary": summary}


@app.delete("/api/rules/{rule_id}")
async def api_rule_delete(rule_id: int, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    row = await runtime.db.get_rule(rule_id)
    await runtime.db.delete_rule(rule_id)
    if row and row.get("source_list") == "manual":
        if row.get("rule_type") == "blacklist":
            await runtime.blocklists.remove_entry(scope=row.get("scope", ""), value=row.get("value", ""))
            await runtime.blocklists.reload(runtime.db)
        elif row.get("rule_type") == "whitelist":
            await runtime.allowlists.remove_entry(scope=row.get("scope", ""), value=row.get("value", ""))
            await runtime.allowlists.reload(runtime.db)
    return {"ok": True}


@app.post("/api/settings/prompt")
async def api_settings_prompt(payload: PromptRequest, request: Request) -> dict[str, Any]:
    submitted = payload.custom_prompt.strip()
    if not submitted or submitted == DEFAULT_SAFE_PROMPT.strip():
        await request.app.state.runtime.db.set_setting("custom_prompt", "")
    else:
        await request.app.state.runtime.db.set_setting("custom_prompt", submitted)
    return {"ok": True}


@app.post("/api/settings/prompt/reset")
async def api_settings_prompt_reset(request: Request) -> dict[str, Any]:
    await request.app.state.runtime.db.set_setting("custom_prompt", "")
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
    await runtime.sync_workers()
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
    await runtime.sync_workers()
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
    await runtime.sync_workers()
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
    await runtime.sync_workers()
    return {"ok": True}


@app.post("/api/settings/webhook")
async def api_settings_webhook(payload: WebhookSettingsRequest, request: Request) -> dict[str, Any]:
    await request.app.state.runtime.db.set_setting("failure_webhook_url", payload.failure_webhook_url.strip())
    return {"ok": True}


@app.post("/api/settings/gemini")
async def api_settings_gemini(payload: GeminiSettingsRequest, request: Request) -> dict[str, Any]:
    runtime: RuntimeState = request.app.state.runtime
    await runtime.db.set_setting("gemini_api_key_runtime", payload.api_key.strip())
    if payload.enabled is not None:
        await runtime.db.set_setting("gemini_enabled", "true" if payload.enabled else "false")
        if not payload.enabled:
            await runtime.db.set_setting("judge_ok", "true")
            await runtime.db.set_setting("last_error", "")
    return {"ok": True}


@app.get("/api/history")
async def api_history(request: Request, page: int = 1) -> dict[str, Any]:
    return await request.app.state.runtime.db.paged_video_decisions(page=page, page_size=50, max_total=500)


@app.get("/api/db/stats")
async def api_db_stats(request: Request) -> dict[str, Any]:
    return await request.app.state.runtime.db.db_stats()


@app.post("/api/admin/purge")
async def api_admin_purge(payload: PurgeRequest, request: Request) -> dict[str, Any]:
    dbi = request.app.state.runtime.db
    deleted = 0
    if payload.target == "analysis_cache":
        deleted = await dbi.purge_analysis_cache()
    elif payload.target == "history":
        deleted = await dbi.purge_history()
    elif payload.target == "all":
        deleted = await dbi.purge_analysis_cache()
        deleted += await dbi.purge_history()
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
