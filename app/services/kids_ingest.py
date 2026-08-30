from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

import websockets

from ..config import Settings
from ..db import Database
from .kids_classifier import KidsClassificationError, OpenCodexKidsClassifier

logger = logging.getLogger("sentinel.kids_ingest")

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_DURATION = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")
_CHANNEL_FROM_LABEL = re.compile(r"\sby\s(.+?)(?:\s[\d,]+ views|\s\d+ views|\s*$)", re.IGNORECASE)


@dataclass(frozen=True)
class KidsVideoCandidate:
    video_id: str
    title: str
    channel_title: str
    thumbnail_url: str
    duration_seconds: int
    source_id: int
    source_reference: str


@dataclass
class IngestReport:
    sources_seen: int = 0
    cards_seen: int = 0
    candidates_created: int = 0
    approved: int = 0
    blocked: int = 0
    uncertain: int = 0
    skipped: int = 0
    errors: int = 0


def _duration_seconds(value: str) -> int:
    match = _DURATION.fullmatch((value or "").strip())
    if not match:
        return 0
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)


def source_url(kind: str, reference: str) -> str:
    raw = reference.strip()
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc.lower() != "www.youtubekids.com":
            raise ValueError("Kids source URLs must stay on www.youtubekids.com")
        path = parsed.path.rstrip("/")
        if kind == "channel" and not path.startswith("/channel/"):
            raise ValueError("channel source URLs must use the Kids channel route")
        if kind == "playlist" and path != "/playlist":
            raise ValueError("playlist source URLs must use the Kids playlist route")
        return raw
    encoded = urllib.parse.quote(raw, safe="")
    if kind == "channel":
        return f"https://www.youtubekids.com/channel/{encoded}"
    if kind == "playlist":
        return f"https://www.youtubekids.com/playlist?list={encoded}"
    raise ValueError("unsupported Kids source kind")


def parse_cards(
    cards: list[dict[str, Any]],
    *,
    source_id: int,
    source_reference: str,
) -> list[KidsVideoCandidate]:
    result: list[KidsVideoCandidate] = []
    seen: set[str] = set()
    for card in cards:
        href = str(card.get("href", ""))
        parsed = urllib.parse.urlparse(href)
        query = urllib.parse.parse_qs(parsed.query)
        video_id = str(query.get("v", [""])[0])
        title = str(card.get("title", "")).strip()
        thumbnail_url = str(card.get("thumbnail_url", "")).strip()
        duration = _duration_seconds(str(card.get("duration", "")))
        if (
            parsed.path != "/watch"
            or not _VIDEO_ID.fullmatch(video_id)
            or video_id in seen
            or not title
            or duration <= 0
            or urllib.parse.urlparse(thumbnail_url).scheme != "https"
            or urllib.parse.urlparse(thumbnail_url).netloc.lower() != "i.ytimg.com"
        ):
            continue
        seen.add(video_id)
        label = str(card.get("label", ""))
        channel_match = _CHANNEL_FROM_LABEL.search(label)
        result.append(
            KidsVideoCandidate(
                video_id=video_id,
                title=title[:500],
                channel_title=(channel_match.group(1).strip() if channel_match else "")[:500],
                thumbnail_url=thumbnail_url.split("?", 1)[0][:2000],
                duration_seconds=duration,
                source_id=source_id,
                source_reference=source_reference,
            )
        )
    return result


class YouTubeKidsCDP:
    """Read-only metadata adapter for the already-running Kids Chromium session."""

    def __init__(self, cdp_url: str = "http://127.0.0.1:9223", wait_seconds: float = 15.0):
        self.cdp_url = cdp_url.rstrip("/")
        self.wait_seconds = wait_seconds

    async def _json(self, path: str) -> dict[str, Any] | list[dict[str, Any]]:
        def load() -> dict[str, Any] | list[dict[str, Any]]:
            with urllib.request.urlopen(f"{self.cdp_url}{path}", timeout=5) as response:
                return json.load(response)

        return await asyncio.to_thread(load)

    async def _command(self, websocket: Any, request_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await websocket.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await websocket.recv())
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} failed")
                return message.get("result", {})

    async def _target(self, target_id: str) -> dict[str, Any]:
        for _ in range(50):
            targets = await self._json("/json/list")
            target = next((item for item in targets if item.get("id") == target_id), None)
            if target and target.get("webSocketDebuggerUrl"):
                return target
            await asyncio.sleep(0.2)
        raise RuntimeError("CDP target did not become available")

    async def cards_for_source(self, kind: str, reference: str) -> list[dict[str, Any]]:
        version = await self._json("/json/version")
        target_url = source_url(kind, reference)
        async with websockets.connect(version["webSocketDebuggerUrl"]) as browser:
            created = await self._command(browser, 1, "Target.createTarget", {"url": target_url})
            target_id = str(created["targetId"])
        try:
            target = await self._target(target_id)
            async with websockets.connect(target["webSocketDebuggerUrl"]) as page:
                await self._command(page, 2, "Runtime.enable")
                expression = """JSON.stringify({
                    ready: document.readyState,
                    cards: Array.from(document.querySelectorAll("ytk-compact-video-renderer")).map(x => ({
                        href: x.querySelector('a[href*="/watch?v="]')?.getAttribute("href") || "",
                        title: x.querySelector(".primary-text span")?.textContent?.trim() || "",
                        label: x.querySelector(".primary-text span")?.getAttribute("aria-label") || "",
                        duration: x.querySelector(".overlay")?.textContent?.trim() || "",
                        thumbnail_url: x.querySelector("img")?.src || ""
                    }))
                })"""
                for _ in range(int(self.wait_seconds / 0.5)):
                    result = await self._command(
                        page,
                        3,
                        "Runtime.evaluate",
                        {"expression": expression, "returnByValue": True},
                    )
                    payload = json.loads(result["result"].get("value", "{}"))
                    if payload.get("cards"):
                        return payload["cards"]
                    await asyncio.sleep(0.5)
                return []
        finally:
            async with websockets.connect(version["webSocketDebuggerUrl"]) as browser:
                await self._command(browser, 4, "Target.closeTarget", {"targetId": target_id})


async def ingest_once(
    db: Database,
    browser: YouTubeKidsCDP,
    classifier: OpenCodexKidsClassifier,
    *,
    max_cards_per_source: int = 48,
) -> IngestReport:
    report = IngestReport()
    sources = [
        source
        for source in await db.catalog_sources_list()
        if source.get("state") == "approved"
    ]
    report.sources_seen = len(sources)
    for source in sources:
        try:
            raw_cards = await browser.cards_for_source(source["kind"], source["reference"])
            report.cards_seen += len(raw_cards)
            candidates = parse_cards(
                raw_cards[:max_cards_per_source],
                source_id=int(source["id"]),
                source_reference=str(source["reference"]),
            )
        except Exception:
            report.errors += 1
            logger.exception("Kids source ingest failed source_id=%s", source.get("id"))
            continue

        for candidate in candidates:
            existing = await db.catalog_item_by_video(candidate.video_id)
            if existing and existing.get("state") == "revoked":
                report.skipped += 1
                continue
            if existing and existing.get("state") == "approved":
                report.skipped += 1
                continue
            if existing:
                item = existing
            else:
                item = await db.catalog_create(
                    "item",
                    {
                        "video_id": candidate.video_id,
                        "title": candidate.title,
                        "source_id": candidate.source_id,
                        "thumbnail_url": candidate.thumbnail_url,
                        "duration_seconds": candidate.duration_seconds,
                        "visual_category": "general",
                        "correlation_id": f"kids-ingest-{candidate.video_id}",
                    },
                )
                report.candidates_created += 1

            metadata = asdict(candidate)
            try:
                decision = await classifier.classify(metadata)
            except KidsClassificationError:
                decision = {"verdict": "UNCERTAIN", "reason": "OpenCodex unavailable"}
            except Exception:
                decision = {"verdict": "UNCERTAIN", "reason": "classifier failure"}

            verdict = decision["verdict"]
            if verdict == "SAFE":
                target_state = "approved"
                report.approved += 1
            elif verdict == "UNSAFE":
                target_state = "blocked"
                report.blocked += 1
            else:
                target_state = "unknown"
                report.uncertain += 1
            await db.catalog_transition(
                "item",
                int(item["id"]),
                {
                    "state": target_state,
                    "actor": "kids-ingest",
                    "reason": str(decision.get("reason", ""))[:1000] or "Kids classification",
                    "correlation_id": f"kids-classify-{candidate.video_id}",
                },
            )
    return report


async def _main() -> None:
    settings = Settings()
    db = Database(settings.db_path)
    await db.init()
    classifier = OpenCodexKidsClassifier(
        base_url=settings.opencodex_base_url,
        model=settings.opencodex_model,
    )
    try:
        report = await ingest_once(
            db,
            YouTubeKidsCDP(os.getenv("KIDS_BROWSER_CDP_URL", "http://127.0.0.1:9223")),
            classifier,
        )
        print(json.dumps(asdict(report), sort_keys=True))
    finally:
        await classifier.close()


if __name__ == "__main__":
    asyncio.run(_main())
