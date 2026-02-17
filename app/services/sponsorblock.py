from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from time import monotonic
from typing import Any

import aiohttp

from ..config import Settings


@dataclass
class SegmentCacheEntry:
    expires_at: float
    segments: list[dict[str, Any]] = field(default_factory=list)


class SponsorBlockService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[str, SegmentCacheEntry] = {}
        self._cache_lock = asyncio.Lock()
        self._skip_guard: dict[str, float] = {}

    async def prefetch(self, *, video_id: str, categories: list[str], min_length: float) -> None:
        if not video_id:
            return
        await self.get_segments(video_id=video_id, categories=categories, min_length=min_length)

    async def try_skip_current(
        self,
        *,
        device_id: int,
        video_id: str,
        current_time: float | None,
        categories: list[str],
        min_length: float,
        lounge_seek,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        if current_time is None:
            return False, "", None
        segments = await self.get_segments(video_id=video_id, categories=categories, min_length=min_length)
        if not segments:
            return False, "", None
        selected = self._select_segment(segments, current_time)
        if not selected:
            return False, "", None

        guard_key = f"{device_id}:{video_id}:{selected['end']:.2f}"
        now = monotonic()
        last = self._skip_guard.get(guard_key, 0.0)
        if now - last < 2.0:
            return False, "", selected
        self._skip_guard[guard_key] = now

        seek_to = max(selected["end"] + 0.08, current_time + 0.1)
        ok, err = await lounge_seek(device_id, seek_to)
        return ok, err, selected

    async def get_segments(self, *, video_id: str, categories: list[str], min_length: float) -> list[dict[str, Any]]:
        now = monotonic()
        async with self._cache_lock:
            cached = self._cache.get(video_id)
            if cached and cached.expires_at > now:
                return cached.segments

        fetched = await self._fetch_segments(video_id=video_id, categories=categories, min_length=min_length)
        async with self._cache_lock:
            self._cache[video_id] = SegmentCacheEntry(
                expires_at=now + max(30, self.settings.sponsorblock_segment_cache_ttl_seconds),
                segments=fetched,
            )
        return fetched

    async def _fetch_segments(self, *, video_id: str, categories: list[str], min_length: float) -> list[dict[str, Any]]:
        prefix = hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:4]
        url = f"{self.settings.sponsorblock_api_base.rstrip('/')}/skipSegments/{prefix}"
        params: list[tuple[str, str]] = [("service", "YouTube"), ("actionType", "skip")]
        for cat in categories:
            params.append(("category", cat))
        timeout = aiohttp.ClientTimeout(total=6)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, params=params, headers={"Accept": "application/json"}) as resp:
                    if resp.status != 200:
                        return []
                    payload = await resp.json()
        except Exception:
            return []

        if not isinstance(payload, list):
            return []
        target = None
        for item in payload:
            if isinstance(item, dict) and str(item.get("videoID", "")) == video_id:
                target = item
                break
        if not target:
            return []

        raw_segments = target.get("segments")
        if not isinstance(raw_segments, list):
            return []

        parsed: list[dict[str, Any]] = []
        for seg in raw_segments:
            if not isinstance(seg, dict):
                continue
            pair = seg.get("segment")
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            try:
                start = float(pair[0])
                end = float(pair[1])
            except Exception:
                continue
            if end <= start:
                continue
            if (end - start) < min_length:
                continue
            parsed.append(
                {
                    "start": start,
                    "end": end,
                    "category": str(seg.get("category", "")),
                    "uuid": str(seg.get("UUID", "")),
                }
            )

        if not parsed:
            return []
        parsed.sort(key=lambda x: (x["start"], x["end"]))
        return self._merge_segments(parsed)

    @staticmethod
    def _merge_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for seg in segments:
            if not merged:
                merged.append(seg)
                continue
            prev = merged[-1]
            if seg["start"] <= prev["end"] + 0.8:
                prev["end"] = max(prev["end"], seg["end"])
                if not prev.get("category") and seg.get("category"):
                    prev["category"] = seg["category"]
            else:
                merged.append(seg)
        return merged

    @staticmethod
    def _select_segment(segments: list[dict[str, Any]], position: float) -> dict[str, Any] | None:
        for seg in segments:
            if (seg["start"] - 0.1) <= position < (seg["end"] - 0.05):
                return seg
        return None
