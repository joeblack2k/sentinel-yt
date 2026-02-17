from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

try:
    import paho.mqtt.client as mqtt
except Exception:  # pragma: no cover - optional dependency in some dev envs
    mqtt = None


logger = logging.getLogger("sentinel.mqtt")


def _bool_from_setting(raw: str, default: bool = False) -> bool:
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _safe_int(raw: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(raw).strip())
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def _topic_slug(raw: str, default: str) -> str:
    out = re.sub(r"[^a-zA-Z0-9_/-]+", "", (raw or "").strip())
    out = out.strip("/")
    return out or default


def _switch_payload(value: bool) -> str:
    return "ON" if value else "OFF"


@dataclass
class MQTTConfig:
    enabled: bool
    host: str
    port: int
    username: str
    password: str
    base_topic: str
    discovery_prefix: str
    retain: bool
    tls: bool
    publish_interval_seconds: int
    client_id: str


class MQTTBridge:
    def __init__(self, settings: Any):
        self.settings = settings
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._connected = False
        self._last_error = ""
        self._command_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue(maxsize=256)
        self._config = MQTTConfig(
            enabled=False,
            host="",
            port=1883,
            username="",
            password="",
            base_topic="sentinel",
            discovery_prefix="homeassistant",
            retain=True,
            tls=False,
            publish_interval_seconds=30,
            client_id="sentinel",
        )
        self._config_signature = ""
        self._discovery_signature = ""

    def set_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def publish_interval_seconds(self) -> int:
        return max(5, int(self._config.publish_interval_seconds))

    def info(self) -> dict[str, Any]:
        return {
            "enabled": self._config.enabled,
            "connected": self._connected,
            "host": self._config.host,
            "port": self._config.port,
            "base_topic": self._config.base_topic,
            "discovery_prefix": self._config.discovery_prefix,
            "retain": self._config.retain,
            "tls": self._config.tls,
            "publish_interval_seconds": self._config.publish_interval_seconds,
            "command_topics": self.command_topics(),
            "last_error": self._last_error,
        }

    def command_topics(self) -> dict[str, str]:
        base = self._config.base_topic
        return {
            "active": f"{base}/command/active/set",
            "sponsorblock_active": f"{base}/command/sponsorblock_active/set",
            "remote_release_minutes": f"{base}/command/remote_release_minutes/set",
        }

    def _build_config(self, settings_map: dict[str, str]) -> MQTTConfig:
        host = (settings_map.get("mqtt_host") or "").strip()
        base_topic = _topic_slug(settings_map.get("mqtt_base_topic", "sentinel"), "sentinel")
        discovery_prefix = _topic_slug(settings_map.get("mqtt_discovery_prefix", "homeassistant"), "homeassistant")
        client_id = _topic_slug(settings_map.get("mqtt_client_id", "sentinel-yt"), "sentinel-yt")
        return MQTTConfig(
            enabled=_bool_from_setting(settings_map.get("mqtt_enabled", "false"), False),
            host=host,
            port=_safe_int(settings_map.get("mqtt_port", "1883"), 1883, 1, 65535),
            username=(settings_map.get("mqtt_username") or "").strip(),
            password=settings_map.get("mqtt_password") or "",
            base_topic=base_topic,
            discovery_prefix=discovery_prefix,
            retain=_bool_from_setting(settings_map.get("mqtt_retain", "true"), True),
            tls=_bool_from_setting(settings_map.get("mqtt_tls", "false"), False),
            publish_interval_seconds=_safe_int(
                settings_map.get("mqtt_publish_interval_seconds", "30"),
                30,
                5,
                3600,
            ),
            client_id=client_id,
        )

    @staticmethod
    def _signature(cfg: MQTTConfig) -> str:
        return json.dumps(
            {
                "enabled": cfg.enabled,
                "host": cfg.host,
                "port": cfg.port,
                "username": cfg.username,
                "password": cfg.password,
                "base_topic": cfg.base_topic,
                "discovery_prefix": cfg.discovery_prefix,
                "retain": cfg.retain,
                "tls": cfg.tls,
                "publish_interval_seconds": cfg.publish_interval_seconds,
                "client_id": cfg.client_id,
            },
            sort_keys=True,
        )

    async def apply_settings(self, settings_map: dict[str, str]) -> None:
        cfg = self._build_config(settings_map)
        signature = self._signature(cfg)
        self._config = cfg

        if not cfg.enabled:
            self._last_error = ""
            await self._disconnect()
            self._config_signature = signature
            self._discovery_signature = ""
            return

        if mqtt is None:
            self._last_error = "paho-mqtt is not installed in this build."
            return

        if not cfg.host:
            self._last_error = "MQTT is enabled but broker host is empty."
            await self._disconnect()
            self._config_signature = signature
            self._discovery_signature = ""
            return

        if signature == self._config_signature and self._client is not None:
            return

        await self._disconnect()
        self._config_signature = signature
        self._discovery_signature = ""
        await self._connect(cfg)

    async def _connect(self, cfg: MQTTConfig) -> None:
        if mqtt is None:
            return

        def _connect_blocking() -> Any:
            client = mqtt.Client(client_id=cfg.client_id, clean_session=True)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            if cfg.username:
                client.username_pw_set(cfg.username, cfg.password or None)
            if cfg.tls:
                client.tls_set()
            client.connect(cfg.host, cfg.port, keepalive=45)
            client.loop_start()
            return client

        try:
            self._client = await asyncio.to_thread(_connect_blocking)
            self._last_error = ""
        except Exception as err:
            self._client = None
            self._connected = False
            self._last_error = f"MQTT connect failed: {err}"

    async def _disconnect(self) -> None:
        client = self._client
        self._client = None
        self._connected = False
        if not client:
            return

        def _disconnect_blocking() -> None:
            try:
                client.loop_stop()
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception:
                pass

        await asyncio.to_thread(_disconnect_blocking)

    def _on_connect(self, client: Any, _userdata: Any, _flags: Any, rc: int, _properties: Any = None) -> None:
        self._connected = (rc == 0)
        if not self._connected:
            self._last_error = f"MQTT broker rejected connection (rc={rc})."
            return
        self._last_error = ""
        for _name, topic in self.command_topics().items():
            try:
                client.subscribe(topic, qos=1)
            except Exception:
                pass

    def _on_disconnect(self, _client: Any, _userdata: Any, _rc: int, _properties: Any = None) -> None:
        self._connected = False

    def _enqueue_command(self, command: str, payload: str) -> None:
        try:
            self._command_queue.put_nowait((command, payload))
        except asyncio.QueueFull:
            logger.warning("MQTT command queue full; dropping command %s", command)

    def _on_message(self, _client: Any, _userdata: Any, msg: Any) -> None:
        payload = ""
        try:
            payload = msg.payload.decode("utf-8", errors="ignore").strip()
        except Exception:
            payload = ""
        topic = str(getattr(msg, "topic", "") or "")
        retained = bool(getattr(msg, "retain", False))
        commands = self.command_topics()
        command_name = ""
        if topic == commands["active"]:
            command_name = "active"
        elif topic == commands["sponsorblock_active"]:
            command_name = "sponsorblock_active"
        elif topic == commands["remote_release_minutes"]:
            command_name = "remote_release_minutes"
        if not command_name:
            return
        # Ignore retained commands to avoid replaying stale ON/OFF actions on reconnect.
        if retained:
            return
        if not payload:
            return

        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._enqueue_command, command_name, payload)
        else:
            self._enqueue_command(command_name, payload)

    async def drain_commands(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        while True:
            try:
                out.append(self._command_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return out

    async def _publish(self, topic: str, payload: str, retain: bool | None = None) -> bool:
        if mqtt is None or self._client is None:
            return False
        retain_flag = self._config.retain if retain is None else retain

        def _publish_blocking() -> bool:
            info = self._client.publish(topic, payload=payload, qos=1, retain=retain_flag)
            return int(getattr(info, "rc", 1)) == int(getattr(mqtt, "MQTT_ERR_SUCCESS", 0))

        try:
            ok = await asyncio.to_thread(_publish_blocking)
            if not ok:
                self._last_error = f"MQTT publish failed for topic {topic}."
            return ok
        except Exception as err:
            self._last_error = f"MQTT publish exception: {err}"
            return False

    def _discovery_topic(self, component: str, object_id: str) -> str:
        node = _topic_slug(self._config.client_id, "sentinel-yt")
        return f"{self._config.discovery_prefix}/{component}/{node}/{object_id}/config"

    def _state_topic(self, key: str) -> str:
        return f"{self._config.base_topic}/state/{key}"

    async def publish_discovery(self, *, build_version: str, force: bool = False) -> None:
        if not self._config.enabled or not self._config.host or self._client is None:
            return
        signature = json.dumps(
            {
                "base_topic": self._config.base_topic,
                "discovery_prefix": self._config.discovery_prefix,
                "retain": self._config.retain,
                "build_version": build_version,
            },
            sort_keys=True,
        )
        if (not force) and signature == self._discovery_signature:
            return

        node = _topic_slug(self._config.client_id, "sentinel-yt")
        device = {
            "identifiers": [f"{node}_device"],
            "name": "Sentinel YouTube Guardian",
            "manufacturer": "Sentinel",
            "model": "sentinel-yt",
            "sw_version": build_version,
        }
        availability_topic = self._state_topic("availability")

        entities: list[tuple[str, str, dict[str, Any]]] = [
            (
                "switch",
                "sentinel_active",
                {
                    "name": "Sentinel Active",
                    "unique_id": f"{node}_sentinel_active",
                    "state_topic": self._state_topic("active"),
                    "command_topic": f"{self._config.base_topic}/command/active/set",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "state_on": "ON",
                    "state_off": "OFF",
                    "icon": "mdi:shield-check",
                },
            ),
            (
                "switch",
                "sponsorblock_active",
                {
                    "name": "SponsorBlock Active",
                    "unique_id": f"{node}_sponsorblock_active",
                    "state_topic": self._state_topic("sponsorblock_active"),
                    "command_topic": f"{self._config.base_topic}/command/sponsorblock_active/set",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "state_on": "ON",
                    "state_off": "OFF",
                    "icon": "mdi:skip-next-circle",
                },
            ),
            (
                "binary_sensor",
                "monitoring_effective",
                {
                    "name": "Sentinel Monitoring Effective",
                    "unique_id": f"{node}_monitoring_effective",
                    "state_topic": self._state_topic("monitoring_effective"),
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "icon": "mdi:shield-search",
                },
            ),
            (
                "binary_sensor",
                "sponsorblock_effective",
                {
                    "name": "SponsorBlock Effective",
                    "unique_id": f"{node}_sponsorblock_effective",
                    "state_topic": self._state_topic("sponsorblock_effective"),
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "icon": "mdi:skip-forward-outline",
                },
            ),
            (
                "binary_sensor",
                "judge_ok",
                {
                    "name": "Sentinel Judge OK",
                    "unique_id": f"{node}_judge_ok",
                    "state_topic": self._state_topic("judge_ok"),
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "icon": "mdi:robot",
                },
            ),
            (
                "binary_sensor",
                "schedule_active_now",
                {
                    "name": "Sentinel Schedule Active",
                    "unique_id": f"{node}_schedule_active_now",
                    "state_topic": self._state_topic("schedule_active_now"),
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "icon": "mdi:calendar-clock",
                },
            ),
            (
                "binary_sensor",
                "remote_release_active",
                {
                    "name": "Sentinel Remote Release Active",
                    "unique_id": f"{node}_remote_release_active",
                    "state_topic": self._state_topic("remote_release_active"),
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "icon": "mdi:television-play",
                },
            ),
            (
                "sensor",
                "schedule_mode_now",
                {
                    "name": "Sentinel Schedule Mode",
                    "unique_id": f"{node}_schedule_mode_now",
                    "state_topic": self._state_topic("schedule_mode_now"),
                    "icon": "mdi:timeline-text",
                },
            ),
            (
                "sensor",
                "timezone",
                {
                    "name": "Sentinel Timezone",
                    "unique_id": f"{node}_timezone",
                    "state_topic": self._state_topic("timezone"),
                    "icon": "mdi:map-clock",
                },
            ),
            (
                "sensor",
                "build_version",
                {
                    "name": "Sentinel Build Version",
                    "unique_id": f"{node}_build_version",
                    "state_topic": self._state_topic("build_version"),
                    "icon": "mdi:source-branch",
                },
            ),
            (
                "sensor",
                "blocked_today",
                {
                    "name": "Sentinel Blocked Today",
                    "unique_id": f"{node}_blocked_today",
                    "state_topic": self._state_topic("blocked_today"),
                    "state_class": "measurement",
                    "icon": "mdi:shield-remove",
                },
            ),
            (
                "sensor",
                "blocked_7d",
                {
                    "name": "Sentinel Blocked 7d",
                    "unique_id": f"{node}_blocked_7d",
                    "state_topic": self._state_topic("blocked_7d"),
                    "state_class": "measurement",
                    "icon": "mdi:calendar-week",
                },
            ),
            (
                "sensor",
                "allowed_today",
                {
                    "name": "Sentinel Allowed Today",
                    "unique_id": f"{node}_allowed_today",
                    "state_topic": self._state_topic("allowed_today"),
                    "state_class": "measurement",
                    "icon": "mdi:shield-check",
                },
            ),
            (
                "sensor",
                "allowed_7d",
                {
                    "name": "Sentinel Allowed 7d",
                    "unique_id": f"{node}_allowed_7d",
                    "state_topic": self._state_topic("allowed_7d"),
                    "state_class": "measurement",
                    "icon": "mdi:calendar-week",
                },
            ),
            (
                "sensor",
                "reviewed_today",
                {
                    "name": "Sentinel Reviewed Today",
                    "unique_id": f"{node}_reviewed_today",
                    "state_topic": self._state_topic("reviewed_today"),
                    "state_class": "measurement",
                    "icon": "mdi:counter",
                },
            ),
            (
                "sensor",
                "reviewed_7d",
                {
                    "name": "Sentinel Reviewed 7d",
                    "unique_id": f"{node}_reviewed_7d",
                    "state_topic": self._state_topic("reviewed_7d"),
                    "state_class": "measurement",
                    "icon": "mdi:calendar-week",
                },
            ),
            (
                "sensor",
                "devices_connected",
                {
                    "name": "Sentinel Devices Connected",
                    "unique_id": f"{node}_devices_connected",
                    "state_topic": self._state_topic("devices_connected"),
                    "state_class": "measurement",
                    "icon": "mdi:cast-connected",
                },
            ),
            (
                "sensor",
                "devices_total",
                {
                    "name": "Sentinel Devices Total",
                    "unique_id": f"{node}_devices_total",
                    "state_topic": self._state_topic("devices_total"),
                    "state_class": "measurement",
                    "icon": "mdi:television",
                },
            ),
            (
                "sensor",
                "schedules_count",
                {
                    "name": "Sentinel Schedules Count",
                    "unique_id": f"{node}_schedules_count",
                    "state_topic": self._state_topic("schedules_count"),
                    "state_class": "measurement",
                    "icon": "mdi:calendar-multiselect",
                },
            ),
            (
                "sensor",
                "blocked_total",
                {
                    "name": "Sentinel Blocked Total",
                    "unique_id": f"{node}_blocked_total",
                    "state_topic": self._state_topic("blocked_total"),
                    "state_class": "total_increasing",
                    "icon": "mdi:shield-lock",
                },
            ),
            (
                "sensor",
                "allowed_total",
                {
                    "name": "Sentinel Allowed Total",
                    "unique_id": f"{node}_allowed_total",
                    "state_topic": self._state_topic("allowed_total"),
                    "state_class": "total_increasing",
                    "icon": "mdi:playlist-check",
                },
            ),
            (
                "sensor",
                "db_size_bytes",
                {
                    "name": "Sentinel DB Size",
                    "unique_id": f"{node}_db_size_bytes",
                    "state_topic": self._state_topic("db_size_bytes"),
                    "state_class": "measurement",
                    "unit_of_measurement": "B",
                    "icon": "mdi:database",
                },
            ),
            (
                "sensor",
                "last_error",
                {
                    "name": "Sentinel Last Error",
                    "unique_id": f"{node}_last_error",
                    "state_topic": self._state_topic("last_error"),
                    "icon": "mdi:alert-circle-outline",
                },
            ),
            (
                "number",
                "remote_release_minutes",
                {
                    "name": "Sentinel Release Minutes",
                    "unique_id": f"{node}_remote_release_minutes",
                    "state_topic": self._state_topic("remote_release_minutes"),
                    "command_topic": f"{self._config.base_topic}/command/remote_release_minutes/set",
                    "min": 0,
                    "max": 240,
                    "step": 1,
                    "mode": "box",
                    "icon": "mdi:timer-cog",
                },
            ),
        ]

        for component, object_id, payload in entities:
            discovery_payload = {
                **payload,
                "availability_topic": availability_topic,
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device,
            }
            topic = self._discovery_topic(component, object_id)
            await self._publish(topic, json.dumps(discovery_payload), retain=True)

        self._discovery_signature = signature

    async def publish_snapshot(self, payload: dict[str, Any]) -> None:
        if not self._config.enabled or not self._config.host or self._client is None:
            return
        await self._publish(self._state_topic("availability"), "online", retain=True)
        pairs = {
            "active": _switch_payload(bool(payload.get("active", False))),
            "sponsorblock_active": _switch_payload(bool(payload.get("sponsorblock_active", False))),
            "monitoring_effective": _switch_payload(bool(payload.get("monitoring_effective", False))),
            "sponsorblock_effective": _switch_payload(bool(payload.get("sponsorblock_effective", False))),
            "judge_ok": _switch_payload(bool(payload.get("judge_ok", False))),
            "schedule_active_now": _switch_payload(bool(payload.get("schedule_active_now", False))),
            "schedule_mode_now": str(payload.get("schedule_mode_now", "blocklist")),
            "schedules_count": str(int(payload.get("schedules_count", 0))),
            "timezone": str(payload.get("timezone", "UTC")),
            "build_version": str(payload.get("build_version", "")),
            "remote_release_active": _switch_payload(bool(payload.get("remote_release_active", False))),
            "devices_connected": str(int(payload.get("devices_connected", 0))),
            "devices_total": str(int(payload.get("devices_total", 0))),
            "blocked_today": str(int(payload.get("blocked_today", 0))),
            "blocked_7d": str(int(payload.get("blocked_7d", 0))),
            "allowed_today": str(int(payload.get("allowed_today", 0))),
            "allowed_7d": str(int(payload.get("allowed_7d", 0))),
            "reviewed_today": str(int(payload.get("reviewed_today", 0))),
            "reviewed_7d": str(int(payload.get("reviewed_7d", 0))),
            "blocked_total": str(int(payload.get("blocked_total", 0))),
            "allowed_total": str(int(payload.get("allowed_total", 0))),
            "db_size_bytes": str(int(payload.get("db_size_bytes", 0))),
            "remote_release_minutes": str(int(payload.get("remote_release_minutes", 0))),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "last_error": str(payload.get("last_error", "") or ""),
        }
        for key, value in pairs.items():
            await self._publish(self._state_topic(key), value)

    async def close(self) -> None:
        await self._disconnect()
