from __future__ import annotations

import asyncio
import hashlib
import socket
from urllib.parse import urlparse
from xml.etree import ElementTree
from typing import Any

import aiohttp
from pyytlounge.dial import get_screen_id_from_dial

DIAL_ST = "urn:dial-multiscreen-org:service:dial:1"
MULTICAST_ADDR = "239.255.255.250"
MULTICAST_PORT = 1900


def _first_xml_text(root: ElementTree.Element, paths: list[str]) -> str:
    for path in paths:
        node = root.find(path)
        if node is not None and node.text:
            return node.text.strip()
    return ""


def _looks_like_apple_tv(*, server: str, manufacturer: str, model_name: str, friendly_name: str) -> bool:
    hay = " ".join([server, manufacturer, model_name, friendly_name]).lower()
    needles = ["apple", "apple tv", "appletv", "tvos", "airplay"]
    return any(n in hay for n in needles)


def _parse_ssdp_response(raw: bytes) -> dict[str, str]:
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def _ssdp_scan_sync(timeout_seconds: float, max_results: int) -> list[dict[str, str]]:
    message = "\r\n".join(
        [
            "M-SEARCH * HTTP/1.1",
            f"HOST: {MULTICAST_ADDR}:{MULTICAST_PORT}",
            "MAN: \"ssdp:discover\"",
            "MX: 2",
            f"ST: {DIAL_ST}",
            "",
            "",
        ]
    ).encode("utf-8")

    seen: set[str] = set()
    out: list[dict[str, str]] = []

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout_seconds)
    sock.sendto(message, (MULTICAST_ADDR, MULTICAST_PORT))

    try:
        while len(out) < max_results:
            try:
                raw, _addr = sock.recvfrom(8192)
            except TimeoutError:
                break
            headers = _parse_ssdp_response(raw)
            location = headers.get("location", "")
            usn = headers.get("usn", "")
            key = f"{location}|{usn}"
            if not location or key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "location": location,
                    "usn": usn,
                    "st": headers.get("st", ""),
                    "server": headers.get("server", ""),
                }
            )
    finally:
        sock.close()

    return out


class DiscoveryService:
    async def _fetch_device_description(self, location: str) -> dict[str, str]:
        timeout = aiohttp.ClientTimeout(total=4)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(location) as response:
                    if response.status != 200:
                        return {"friendly_name": "", "manufacturer": "", "model_name": ""}
                    body = await response.text()
            root = ElementTree.fromstring(body)
            # Accept both namespaced and non-namespaced XML variants.
            friendly_name = _first_xml_text(
                root,
                [
                    ".//{urn:schemas-upnp-org:device-1-0}friendlyName",
                    ".//friendlyName",
                ],
            )
            manufacturer = _first_xml_text(
                root,
                [
                    ".//{urn:schemas-upnp-org:device-1-0}manufacturer",
                    ".//manufacturer",
                ],
            )
            model_name = _first_xml_text(
                root,
                [
                    ".//{urn:schemas-upnp-org:device-1-0}modelName",
                    ".//modelName",
                ],
            )
            return {
                "friendly_name": friendly_name,
                "manufacturer": manufacturer,
                "model_name": model_name,
            }
        except Exception:
            return {"friendly_name": "", "manufacturer": "", "model_name": ""}

    async def scan(self, timeout_seconds: float = 2.5, max_results: int = 30) -> list[dict[str, Any]]:
        base = await asyncio.to_thread(_ssdp_scan_sync, timeout_seconds, max_results)
        enriched: list[dict[str, Any]] = []
        for item in base:
            location = item["location"]
            host = urlparse(location).hostname or ""
            dial_result = None
            try:
                dial_result = await get_screen_id_from_dial(location)
            except Exception:
                dial_result = None
            description = await self._fetch_device_description(location)

            screen_id = dial_result.screen_id if dial_result else ""
            screen_name = dial_result.screen_name if dial_result else ""
            friendly_name = description["friendly_name"]
            manufacturer = description["manufacturer"]
            model_name = description["model_name"]

            raw_ref = f"{location}|{screen_id or item.get('usn','')}"
            ref_hash = hashlib.sha1(raw_ref.encode("utf-8")).hexdigest()[:12]
            device_ref = f"{host or 'device'}-{ref_hash}"

            display_name = screen_name or friendly_name or model_name or host or "Unknown TV"

            probable_apple_tv = _looks_like_apple_tv(
                server=item.get("server", ""),
                manufacturer=manufacturer,
                model_name=model_name,
                friendly_name=friendly_name,
            )
            enriched.append(
                {
                    **item,
                    "host": host,
                    "screen_id": screen_id,
                    "screen_name": screen_name,
                    "friendly_name": friendly_name,
                    "manufacturer": manufacturer,
                    "model_name": model_name,
                    "display_name": display_name,
                    "probable_apple_tv": probable_apple_tv,
                    "device_ref": device_ref,
                }
            )
        # Keep clear candidate set: prefer items that have a lounge screen_id or look like Apple TV.
        filtered = [d for d in enriched if d.get("screen_id") or d.get("probable_apple_tv")]
        target = filtered if filtered else enriched

        # Bubble up likely pairable targets first.
        return sorted(
            target,
            key=lambda d: (
                0 if d.get("screen_id") else 1,
                0 if d.get("probable_apple_tv") else 1,
                (d.get("display_name") or "").lower(),
            ),
        )
