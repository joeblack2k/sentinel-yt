from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

from ..config import Settings
from ..db import Database

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^(UC[A-Za-z0-9_-]{22}|@[A-Za-z0-9_.-]+)$")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class BlocklistSnapshot:
    video_ids: set[str] = field(default_factory=set)
    channel_ids: set[str] = field(default_factory=set)
    entries: list[dict[str, str]] = field(default_factory=list)
    loaded_at: str = ""
    remote_sources: list[str] = field(default_factory=list)


class BlocklistService:
    def __init__(self, settings: Settings, *, list_kind: str = "blacklist"):
        if list_kind not in {"blacklist", "whitelist"}:
            raise ValueError("list_kind must be 'blacklist' or 'whitelist'")
        self.settings = settings
        self.list_kind = list_kind
        self._lock = asyncio.Lock()
        self._snapshot = BlocklistSnapshot()
        self._sources_setting_key = f"{list_kind}_source_urls"
        filename = f"custom-{list_kind}.txt"
        self._local_path = Path(settings.data_dir) / "blocklists" / filename
        self._fallback_path = Path(settings.db_path).parent / "blocklists" / filename

    @property
    def local_path(self) -> Path:
        return self._local_path

    async def _activate_fallback_path(self) -> None:
        if self._local_path == self._fallback_path:
            return
        self._local_path = self._fallback_path
        self.local_path.parent.mkdir(parents=True, exist_ok=True)

    async def ensure_local_file(self) -> None:
        try:
            self.local_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            await self._activate_fallback_path()
        if self.local_path.exists():
            return
        try:
            self.local_path.write_text(
                (
                    f"# Sentinel {self.list_kind.capitalize()} File v1\n"
                    "# Supported entry formats:\n"
                    "# 1) video:<VIDEO_ID> | Human readable title | https://www.youtube.com/watch?v=<VIDEO_ID>\n"
                    "# 2) channel:<CHANNEL_ID_OR_HANDLE> | Channel name | https://www.youtube.com/channel/<CHANNEL_ID>\n"
                    "# 3) Direct YouTube links are accepted and parsed.\n"
                    "# Lines starting with # are comments.\n"
                ),
                encoding="utf-8",
            )
        except OSError:
            await self._activate_fallback_path()
            if not self.local_path.exists():
                self.local_path.write_text(
                    (
                        f"# Sentinel {self.list_kind.capitalize()} File v1\n"
                        "# Supported entry formats:\n"
                        "# 1) video:<VIDEO_ID> | Human readable title | https://www.youtube.com/watch?v=<VIDEO_ID>\n"
                        "# 2) channel:<CHANNEL_ID_OR_HANDLE> | Channel name | https://www.youtube.com/channel/<CHANNEL_ID>\n"
                        "# 3) Direct YouTube links are accepted and parsed.\n"
                        "# Lines starting with # are comments.\n"
                    ),
                    encoding="utf-8",
                )

    async def get_local_content(self) -> str:
        await self.ensure_local_file()
        return self.local_path.read_text(encoding="utf-8")

    async def save_local_content(self, content: str) -> None:
        await self.ensure_local_file()
        self.local_path.write_text(content or "", encoding="utf-8")

    async def set_sources(self, db: Database, urls: list[str]) -> None:
        await db.set_setting(self._sources_setting_key, "\n".join(urls))

    async def get_sources(self, db: Database) -> list[str]:
        raw = (await db.get_setting(self._sources_setting_key)) or ""
        out: list[str] = []
        for line in raw.splitlines():
            item = line.strip()
            if not item:
                continue
            out.append(item)
        return out

    async def append_entry(
        self,
        *,
        scope: str,
        value: str,
        label: str = "",
        url: str = "",
        source_list: str = "manual",
    ) -> None:
        await self.ensure_local_file()
        scope = scope.strip().lower()
        value = value.strip()
        if scope not in {"video", "channel"} or not value:
            return
        safe_label = (label or "").strip().replace("\n", " ").replace("\r", " ")
        safe_url = (url or "").strip()
        comment = f"# [{source_list}] {safe_label}" if safe_label else f"# [{source_list}] {scope}:{value}"
        line = f"{scope}:{value}"
        if safe_label:
            line += f" | {safe_label}"
        if safe_url:
            line += f" | {safe_url}"
        async with self._lock:
            text = await self.get_local_content()
            if f"{scope}:{value}" in text:
                return
            with self.local_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n{comment}\n{line}\n")
            if scope == "video":
                self._snapshot.video_ids.add(value)
            else:
                self._snapshot.channel_ids.add(value)
            self._snapshot.entries.append(
                {
                    "scope": scope,
                    "value": value,
                    "label": safe_label,
                    "url": safe_url,
                    "source_list": source_list,
                }
            )

    async def remove_entry(self, *, scope: str, value: str) -> None:
        scope = scope.strip().lower()
        value = value.strip()
        if scope not in {"video", "channel"} or not value:
            return
        await self.ensure_local_file()
        async with self._lock:
            lines = self.local_path.read_text(encoding="utf-8").splitlines()
            target = f"{scope}:{value}"
            filtered: list[str] = []
            skip_next_comment = False
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("# [manual]"):
                    skip_next_comment = True
                    filtered.append(line)
                    continue
                if stripped.startswith(target):
                    if skip_next_comment and filtered:
                        filtered.pop()
                    skip_next_comment = False
                    continue
                skip_next_comment = False
                filtered.append(line)
            self.local_path.write_text("\n".join(filtered).rstrip() + "\n", encoding="utf-8")

    async def reload(self, db: Database) -> dict[str, Any]:
        await self.ensure_local_file()
        local_content = await self.get_local_content()
        sources = await self.get_sources(db)
        remote_contents = await self._download_sources(sources)

        merged = [("local", str(self.local_path), local_content)]
        for src, content in remote_contents:
            merged.append(("remote", src, content))

        snapshot = BlocklistSnapshot()
        for source_kind, source_name, content in merged:
            parsed = self._parse_content(content, source_name=source_name)
            snapshot.video_ids.update(parsed["video_ids"])
            snapshot.channel_ids.update(parsed["channel_ids"])
            for entry in parsed["entries"]:
                entry["source_list"] = source_name if source_kind == "remote" else "local"
                snapshot.entries.append(entry)
        snapshot.loaded_at = _utc_now_iso()
        snapshot.remote_sources = sources
        async with self._lock:
            self._snapshot = snapshot
        return self.summary()

    async def match(self, *, video_id: str, channel_id: str) -> dict[str, str] | None:
        async with self._lock:
            snap = self._snapshot
            if video_id and video_id in snap.video_ids:
                return {"rule_type": self.list_kind, "scope": "video", "value": video_id, "source_list": "file"}
            if channel_id and channel_id in snap.channel_ids:
                return {"rule_type": self.list_kind, "scope": "channel", "value": channel_id, "source_list": "file"}
        return None

    def summary(self) -> dict[str, Any]:
        snap = self._snapshot
        return {
            "list_kind": self.list_kind,
            "video_count": len(snap.video_ids),
            "channel_count": len(snap.channel_ids),
            "entries_count": len(snap.entries),
            "loaded_at": snap.loaded_at,
            "local_path": str(self.local_path),
            "sources": snap.remote_sources,
        }

    async def recent_entries(self, limit: int = 10) -> list[dict[str, str]]:
        async with self._lock:
            return list(reversed(self._snapshot.entries[-limit:]))

    async def _download_sources(self, sources: list[str]) -> list[tuple[str, str]]:
        if not sources:
            return []
        timeout = aiohttp.ClientTimeout(total=15)
        out: list[tuple[str, str]] = []
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for src in sources:
                try:
                    async with session.get(src) as resp:
                        if resp.status != 200:
                            continue
                        text = await resp.text()
                        out.append((src, text))
                except Exception:
                    continue
        return out

    def _parse_content(self, content: str, *, source_name: str) -> dict[str, Any]:
        video_ids: set[str] = set()
        channel_ids: set[str] = set()
        entries: list[dict[str, str]] = []
        for raw in content.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parsed = self._parse_line(line)
            if not parsed:
                continue
            scope = parsed["scope"]
            value = parsed["value"]
            if scope == "video":
                video_ids.add(value)
            else:
                channel_ids.add(value)
            entries.append(
                {
                    "scope": scope,
                    "value": value,
                    "label": parsed.get("label", ""),
                    "url": parsed.get("url", ""),
                    "source_list": source_name,
                }
            )
        return {"video_ids": video_ids, "channel_ids": channel_ids, "entries": entries}

    def _parse_line(self, line: str) -> dict[str, str] | None:
        parts = [p.strip() for p in line.split("|")]
        primary = parts[0]
        label = parts[1] if len(parts) > 1 else ""
        url = parts[2] if len(parts) > 2 else ""

        if primary.startswith("video:"):
            vid = primary.split(":", 1)[1].strip()
            if _VIDEO_ID_RE.match(vid):
                if not url:
                    url = f"https://www.youtube.com/watch?v={vid}"
                return {"scope": "video", "value": vid, "label": label, "url": url}
            return None

        if primary.startswith("channel:"):
            ch = primary.split(":", 1)[1].strip()
            if _CHANNEL_ID_RE.match(ch):
                if ch.startswith("UC"):
                    default_url = f"https://www.youtube.com/channel/{ch}"
                else:
                    default_url = f"https://www.youtube.com/{ch}"
                if not url:
                    url = default_url
                return {"scope": "channel", "value": ch, "label": label, "url": url}
            return None

        parsed_from_url = self._extract_from_url(primary)
        if parsed_from_url:
            return parsed_from_url

        token = primary.strip()
        if _VIDEO_ID_RE.match(token):
            return {
                "scope": "video",
                "value": token,
                "label": label,
                "url": url or f"https://www.youtube.com/watch?v={token}",
            }
        if _CHANNEL_ID_RE.match(token):
            if token.startswith("UC"):
                default_url = f"https://www.youtube.com/channel/{token}"
            else:
                default_url = f"https://www.youtube.com/{token}"
            return {"scope": "channel", "value": token, "label": label, "url": url or default_url}
        return None

    def _extract_from_url(self, text: str) -> dict[str, str] | None:
        try:
            parsed = urlparse(text)
        except Exception:
            return None
        host = (parsed.netloc or "").lower()
        if "youtube.com" not in host and "youtu.be" not in host:
            return None

        if "youtu.be" in host:
            vid = parsed.path.strip("/").split("/", 1)[0]
            if _VIDEO_ID_RE.match(vid):
                return {
                    "scope": "video",
                    "value": vid,
                    "label": "",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                }
            return None

        q = parse_qs(parsed.query)
        if "v" in q and q["v"]:
            vid = q["v"][0].strip()
            if _VIDEO_ID_RE.match(vid):
                return {
                    "scope": "video",
                    "value": vid,
                    "label": "",
                    "url": f"https://www.youtube.com/watch?v={vid}",
                }

        path_parts = [p for p in parsed.path.split("/") if p]
        if len(path_parts) >= 2 and path_parts[0] == "channel":
            channel_id = path_parts[1].strip()
            if _CHANNEL_ID_RE.match(channel_id):
                return {
                    "scope": "channel",
                    "value": channel_id,
                    "label": "",
                    "url": f"https://www.youtube.com/channel/{channel_id}",
                }
        if path_parts and path_parts[0].startswith("@"):
            handle = path_parts[0]
            if _CHANNEL_ID_RE.match(handle):
                return {"scope": "channel", "value": handle, "label": "", "url": f"https://www.youtube.com/{handle}"}
        return None
