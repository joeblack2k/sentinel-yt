from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from pyytlounge import EventListener, YtLoungeApi
from pyytlounge.events import AutoplayUpNextEvent, DisconnectedEvent, NowPlayingEvent

from ..config import Settings
from ..db import Database

EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class PairingError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _humanize_lounge_error(err: Exception | str) -> str:
    raw = str(err).strip() if not isinstance(err, str) else err.strip()
    if not raw:
        return "Unknown lounge error."
    lower = raw.lower()
    if "not connected" in lower:
        return "The TV session is not connected yet. Sentinel will retry automatically."
    if "unsupported client" in lower:
        return (
            "The current YouTube client profile on the TV is not supported for remote control. "
            "Switch profile on TV and try again."
        )
    if "refresh_auth_failed" in lower:
        return "The TV pairing token expired. Re-pair this TV using a fresh code."
    if "connect_failed" in lower:
        return "Sentinel could not connect to the TV lounge session. Check that YouTube is open on the TV."
    if "timeout" in lower or "timed out" in lower:
        return "The TV did not respond in time. Please keep YouTube open and retry."
    if "network" in lower or "host" in lower or "connection" in lower:
        return "Network communication with the TV failed. Check local network connectivity."
    if "subscription_ended" in lower:
        return "The TV ended the lounge subscription. Sentinel will reconnect automatically."
    if "disconnected" in lower:
        return "The TV session disconnected. Sentinel is reconnecting automatically."
    return raw


def _normalize_auth_state(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize persisted auth state to pyytlounge AuthState v1 schema."""
    if "version" in data and "screenId" in data:
        return {
            "version": int(data.get("version", 0)),
            "screenId": data.get("screenId"),
            "loungeIdToken": data.get("loungeIdToken"),
            "refreshToken": data.get("refreshToken"),
            "expiry": int(data.get("expiry", 0) or 0),
        }

    # Backward compatibility for legacy keys persisted by wrapper.store_auth_state().
    return {
        "version": 0,
        "screenId": data.get("screenId") or data.get("screen_id"),
        "loungeIdToken": data.get("loungeIdToken") or data.get("lounge_id_token") or data.get("loungeToken"),
        "refreshToken": data.get("refreshToken") or data.get("refresh_token"),
        "expiry": int(data.get("expiry", 0) or 0),
    }


@dataclass
class WorkerState:
    device_id: int
    worker: "DeviceWorker"
    task: asyncio.Task[None]


class _WorkerListener(EventListener):
    def __init__(self, worker: "DeviceWorker"):
        super().__init__()
        self.worker = worker

    async def now_playing_changed(self, event: NowPlayingEvent) -> None:
        await self.worker.handle_now_playing(event)

    async def autoplay_up_next_changed(self, event: AutoplayUpNextEvent) -> None:
        await self.worker.handle_up_next(event)

    async def disconnected(self, event: DisconnectedEvent) -> None:
        await self.worker.handle_disconnected(event)


class DeviceWorker:
    def __init__(
        self,
        *,
        device_id: int,
        db: Database,
        settings: Settings,
        event_callback: EventCallback,
    ):
        self.device_id = device_id
        self.db = db
        self.settings = settings
        self.event_callback = event_callback
        self.stop_event = asyncio.Event()
        self.api: Optional[YtLoungeApi] = None
        self.api_lock = asyncio.Lock()
        self.last_video_id = ""

    async def run(self) -> None:
        backoff = 2
        while not self.stop_event.is_set():
            device = await self.db.get_device(self.device_id)
            if not device:
                return
            auth_json = device.get("auth_state_json") or ""
            if not auth_json:
                await self.db.update_device_status(
                    self.device_id,
                    "offline",
                    "Missing pairing credentials. Please pair this TV again.",
                )
                await asyncio.sleep(5)
                continue

            listener = _WorkerListener(self)

            try:
                async with YtLoungeApi(f"Sentinel-{self.device_id}", listener) as api:
                    async with self.api_lock:
                        self.api = api

                    api.load_auth_state(_normalize_auth_state(json.loads(auth_json)))
                    await self.db.update_device_status(self.device_id, "connecting", "")

                    if not await api.refresh_auth():
                        raise RuntimeError("refresh_auth_failed")

                    new_auth = _normalize_auth_state(api.auth.serialize())
                    await self.db.upsert_device(
                        name=device.get("name") or "",
                        screen_id=device["screen_id"],
                        lounge_token=new_auth.get("loungeIdToken", ""),
                        auth_state=new_auth,
                        status="linked",
                        last_error="",
                    )

                    connected = await api.connect()
                    if not connected:
                        raise RuntimeError("connect_failed")

                    await self.db.update_device_status(self.device_id, "connected", "")
                    await self.event_callback(
                        {
                            "event": "device_status",
                            "device_id": self.device_id,
                            "status": "connected",
                        }
                    )
                    backoff = 2
                    await api.subscribe()
                    await self.db.update_device_status(
                        self.device_id,
                        "offline",
                        _humanize_lounge_error("subscription_ended"),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as err:
                err_msg = _humanize_lounge_error(err)
                await self.db.update_device_status(self.device_id, "offline", err_msg)
                await self.event_callback(
                    {
                        "event": "device_status",
                        "device_id": self.device_id,
                        "status": "offline",
                        "error": err_msg,
                    }
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                async with self.api_lock:
                    self.api = None

    async def stop(self) -> None:
        self.stop_event.set()
        async with self.api_lock:
            api = self.api
        if api:
            try:
                if api.connected():
                    await api.disconnect()
            except Exception:
                pass
            try:
                await api.close()
            except Exception:
                pass

    async def next_video(self) -> tuple[bool, str, str]:
        async with self.api_lock:
            api = self.api
        if not api:
            return False, "No active TV session in the worker. Reconnect in progress.", "none"

        # Prefer seek_to end to avoid some TV clients dropping the session on `next()`.
        try:
            seek_ok = await api.seek_to(99999)
            if seek_ok:
                return True, "", "seek_end"
        except Exception as err:
            seek_err = _humanize_lounge_error(err)
        else:
            seek_err = "Could not fast-forward the current video."

        # Fallback: attempt regular `next()` when seek fails.
        try:
            next_ok = await api.next()
            if next_ok:
                return True, "", "next"
        except Exception as err:
            next_err = _humanize_lounge_error(err)
        else:
            next_err = "The TV did not accept the skip command."
        return False, f"{seek_err} {next_err}".strip(), "none"

    async def seek_video(self, position_seconds: float) -> tuple[bool, str]:
        async with self.api_lock:
            api = self.api
        if not api:
            return False, "No active TV session in the worker. Reconnect in progress."
        try:
            ok = await api.seek_to(position_seconds)
            if ok:
                return True, ""
            return False, "The TV did not accept the seek command."
        except Exception as err:
            return False, _humanize_lounge_error(err)

    async def play_video(self, video_id: str) -> tuple[bool, str]:
        async with self.api_lock:
            api = self.api
        if not api:
            return False, "No active TV session in the worker. Reconnect in progress."
        try:
            ok = await api.play_video(video_id)
            if ok:
                return True, ""
            return False, "The TV did not accept play command for the selected safe video."
        except Exception as err:
            return False, _humanize_lounge_error(err)

    async def handle_now_playing(self, event: NowPlayingEvent) -> None:
        if not event.video_id:
            return
        if event.video_id == self.last_video_id and event.current_time is None:
            return
        self.last_video_id = event.video_id
        await self.event_callback(
            {
                "event": "now_playing",
                "device_id": self.device_id,
                "video_id": event.video_id,
                "current_time": event.current_time,
                "duration": event.duration,
                "play_state": event.state.value if event.state else None,
            }
        )

    async def handle_up_next(self, event: AutoplayUpNextEvent) -> None:
        if not event.video_id:
            return
        await self.event_callback(
            {
                "event": "up_next",
                "device_id": self.device_id,
                "video_id": event.video_id,
            }
        )

    async def handle_disconnected(self, event: DisconnectedEvent) -> None:
        reason = _humanize_lounge_error(event.reason or "disconnected")
        await self.db.update_device_status(self.device_id, "offline", reason)
        await self.event_callback(
            {
                "event": "device_status",
                "device_id": self.device_id,
                "status": "offline",
                "error": reason,
            }
        )


class LoungeManager:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        event_callback: EventCallback,
    ):
        self.db = db
        self.settings = settings
        self.event_callback = event_callback
        self.workers: dict[int, WorkerState] = {}

    async def start_for_existing_devices(self) -> None:
        devices = await self.db.list_devices()
        for dev in devices:
            await self.ensure_worker(dev["id"])

    async def ensure_worker(self, device_id: int) -> None:
        if device_id in self.workers:
            return
        worker = DeviceWorker(
            device_id=device_id,
            db=self.db,
            settings=self.settings,
            event_callback=self.event_callback,
        )
        task = asyncio.create_task(worker.run(), name=f"lounge-worker-{device_id}")
        self.workers[device_id] = WorkerState(device_id=device_id, worker=worker, task=task)

    async def stop_all(self) -> None:
        states = list(self.workers.values())
        self.workers.clear()
        pending: list[asyncio.Task[None]] = []
        for state in states:
            await state.worker.stop()
            if state.task.done():
                continue
            try:
                await asyncio.wait_for(state.task, timeout=3)
            except asyncio.TimeoutError:
                state.task.cancel()
                pending.append(state.task)
            except Exception:
                pass
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def pause_all(self) -> None:
        await self.stop_all()
        devices = await self.db.list_devices()
        for dev in devices:
            await self.db.update_device_status(dev["id"], "paused", "schedule_or_state_inactive")

    async def pair_device(self, pairing_code: str, device_ref: str) -> dict[str, Any]:
        normalized_code = re.sub(r"\D+", "", pairing_code or "")
        if len(normalized_code) < 6:
            raise PairingError(
                "pair_code_invalid",
                "The pairing code looks invalid. Please enter the full code shown on the TV.",
            )

        try:
            async with YtLoungeApi("Sentinel-Pair") as api:
                try:
                    paired = await api.pair(normalized_code)
                except Exception as err:
                    msg = str(err).lower()
                    if "timeout" in msg or "timed out" in msg:
                        raise PairingError(
                            "pair_timeout",
                            "Pairing timed out. Request a new code on the TV and try again.",
                        ) from err
                    if "json" in msg or "pairing failed" in msg or "expecting value" in msg:
                        raise PairingError(
                            "pair_rejected",
                            "The TV rejected the pairing code. Double-check the code and try again.",
                        ) from err
                    raise PairingError(
                        "pair_failed",
                        "Pairing failed due to a network or TV API error. Please try again.",
                    ) from err

                if not paired:
                    raise PairingError(
                        "pair_rejected",
                        "The TV did not accept this pairing code. Please request a new code and retry.",
                    )
                auth = _normalize_auth_state(api.auth.serialize())
                screen_id = auth.get("screenId") or ""
                lounge_token = auth.get("loungeIdToken", "")
                if not screen_id:
                    raise PairingError(
                        "pair_missing_screen_id",
                        "Pairing succeeded but no screen ID was returned by the TV.",
                    )

                device_name = api.screen_name or f"YouTube Screen {screen_id[:6]}"
                device_id = await self.db.upsert_device(
                    name=device_name,
                    screen_id=screen_id,
                    lounge_token=lounge_token,
                    auth_state=auth,
                    status="paired",
                    last_error="",
                )
                return {
                    "device_id": device_id,
                    "screen_id": screen_id,
                    "name": device_name,
                    "device_ref": device_ref,
                }
        except PairingError:
            raise
        except Exception as err:
            msg = str(err).lower()
            if "timeout" in msg or "timed out" in msg:
                raise PairingError(
                    "pair_timeout",
                    "Pairing timed out. Request a new code on the TV and try again.",
                ) from err
            if "connection" in msg or "network" in msg or "host" in msg:
                raise PairingError(
                    "pair_network_error",
                    "The TV could not be reached during pairing. Check network connectivity and try again.",
                ) from err
            raise PairingError(
                "pair_failed",
                "Pairing could not be completed. Request a fresh TV code, keep YouTube open on the TV, and retry.",
            ) from err

    async def next_video(self, device_id: int) -> tuple[bool, str, str]:
        state = self.workers.get(device_id)
        if not state:
            return False, "No active worker for this TV. Sentinel is reconnecting.", "none"
        task = state.task
        if task.done():
            return False, "TV worker is restarting. Sentinel will retry automatically.", "none"
        return await state.worker.next_video()

    async def seek_video(self, device_id: int, position_seconds: float) -> tuple[bool, str]:
        state = self.workers.get(device_id)
        if not state:
            return False, "No active worker for this TV. Sentinel is reconnecting."
        task = state.task
        if task.done():
            return False, "TV worker is restarting. Sentinel will retry automatically."
        return await state.worker.seek_video(position_seconds)

    async def play_video(self, device_id: int, video_id: str) -> tuple[bool, str]:
        state = self.workers.get(device_id)
        if not state:
            return False, "No active worker for this TV. Sentinel is reconnecting."
        task = state.task
        if task.done():
            return False, "TV worker is restarting. Sentinel will retry automatically."
        return await state.worker.play_video(video_id)
