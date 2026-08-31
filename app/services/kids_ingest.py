from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import websockets

from ..config import Settings
from ..db import Database, utc_now_iso
from .blocklists import BlocklistService
from .kids_classifier import KidsClassificationError, OpenCodexKidsClassifier
from .judge import JudgeService
from .webhook import WebhookClient

logger = logging.getLogger("sentinel.kids_ingest")

HOME_SOURCE_REFERENCE = "__youtube_kids_home__"
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_DURATION = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")
_CHANNEL_FROM_LABEL = re.compile(r"\sby\s(.+?)(?:\s[\d,]+ views|\s\d+ views|\s*$)", re.IGNORECASE)
_MAX_COLLECTED_CARDS = 160


@dataclass(frozen=True)
class KidsVideoCandidate:
    video_id: str
    title: str
    channel_title: str
    channel_id: str
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
    channels_screened: int = 0
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
    if kind == "channel" and raw == HOME_SOURCE_REFERENCE:
        return "https://www.youtubekids.com/"
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


def channel_id_from_reference(reference: str) -> str:
    raw = reference.strip()
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme or parsed.netloc:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2 and parts[0] == "channel":
            return parts[1]
    return raw


def channel_evidence(cards: list[dict[str, Any]], *, channel_id: str, limit: int) -> list[dict[str, Any]]:
    candidates = parse_cards(
        cards,
        source_id=0,
        source_reference=channel_id,
        allowed_channel_ids={channel_id},
    )
    return [
        {
            "video_id": candidate.video_id,
            "title": candidate.title,
            "duration_seconds": candidate.duration_seconds,
            "thumbnail_url": candidate.thumbnail_url,
        }
        for candidate in candidates[: max(1, min(int(limit), 20))]
    ]


def source_safety_is_current(
    source: dict[str, Any],
    *,
    policy_version: str,
    recheck_seconds: int,
) -> bool:
    if source.get("safety_policy_version") != policy_version or not source.get("safety_checked_at"):
        return False
    try:
        checked_at = datetime.fromisoformat(str(source["safety_checked_at"]))
    except ValueError:
        return False
    if checked_at.tzinfo is None:
        return False
    return checked_at >= datetime.now(timezone.utc) - timedelta(seconds=max(0, recheck_seconds))


def parse_cards(
    cards: list[dict[str, Any]],
    *,
    source_id: int,
    source_reference: str,
    allowed_channel_ids: set[str] | None = None,
) -> list[KidsVideoCandidate]:
    result: list[KidsVideoCandidate] = []
    seen: set[str] = set()
    for card in cards:
        href = str(card.get("href", ""))
        parsed = urllib.parse.urlparse(href)
        query = urllib.parse.parse_qs(parsed.query)
        video_id = str(query.get("v", [""])[0])
        title = str(card.get("title", "")).strip()
        channel_id = str(card.get("channel_id", "")).strip()
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
            or (allowed_channel_ids is not None and channel_id not in allowed_channel_ids)
        ):
            continue
        seen.add(video_id)
        label = str(card.get("label", ""))
        channel_match = _CHANNEL_FROM_LABEL.search(label)
        channel_title = str(card.get("channel_title", "")).strip()
        result.append(
            KidsVideoCandidate(
                video_id=video_id,
                title=title[:500],
                channel_title=(channel_title or (channel_match.group(1).strip() if channel_match else ""))[:500],
                channel_id=channel_id[:128],
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

    async def _command(
        self,
        websocket: Any,
        request_id: int,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        command_timeout = timeout if timeout is not None else max(1.0, min(5.0, self.wait_seconds))
        deadline = asyncio.get_running_loop().time() + command_timeout
        await asyncio.wait_for(
            websocket.send(json.dumps({"id": request_id, "method": method, "params": params or {}})),
            timeout=command_timeout,
        )
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"CDP {method} timed out")
            message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=remaining))
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
        target_id: str | None = None
        original_error: BaseException | None = None
        try:
            async with websockets.connect(version["webSocketDebuggerUrl"]) as browser:
                created = await self._command(browser, 1, "Target.createTarget", {"url": target_url})
                if not created.get("targetId"):
                    raise RuntimeError("CDP target creation returned no target")
                target_id = str(created["targetId"])
            target = await self._target(target_id)
            async with websockets.connect(target["webSocketDebuggerUrl"]) as page:
                await self._command(page, 2, "Runtime.enable")
                navigation = await self._command(page, 3, "Page.navigate", {"url": target_url})
                if navigation.get("errorText"):
                    raise RuntimeError("CDP navigation failed")
                expression = """(() => {
                  const payload = {
                    url: window.location.href,
                    ready: document.readyState,
                    cards: Array.from(document.querySelectorAll("ytk-compact-video-renderer")).map(x => {
                        const data = x.get("data") || {};
                        return {
                        href: x.querySelector('a[href*="/watch?v="]')?.getAttribute("href") || "",
                        title: x.querySelector(".primary-text span")?.textContent?.trim() || "",
                        label: x.querySelector(".primary-text span")?.getAttribute("aria-label") || "",
                        channel_title: data.shortBylineText?.runs?.map(r => r.text).join("") || "",
                        duration: x.querySelector(".overlay")?.textContent?.trim() || "",
                        thumbnail_url: x.querySelector("img")?.src || "",
                        channel_id: data.kidsVideoOwnerExtension?.externalChannelId
                            || data.shortBylineText?.runs?.[0]?.navigationEndpoint?.browseEndpoint?.browseId
                            || ""
                        };
                    })
                  };
                  window.scrollTo(0, document.documentElement.scrollHeight);
                  return JSON.stringify(payload);
                })()"""
                collected: dict[str, dict[str, Any]] = {}
                stable_rounds = 0
                ready_seen = False
                deadline = asyncio.get_running_loop().time() + max(0.5, self.wait_seconds)
                while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
                    result = await self._command(
                        page,
                        4,
                        "Runtime.evaluate",
                        {"expression": expression, "returnByValue": True},
                        timeout=remaining,
                    )
                    payload = json.loads(result["result"].get("value", "{}"))
                    actual_url = urllib.parse.urlparse(str(payload.get("url", "")))
                    if (
                        actual_url.scheme != "https"
                        or actual_url.netloc.lower() != "www.youtubekids.com"
                    ):
                        if payload.get("url") != "about:blank":
                            raise RuntimeError("CDP navigation left YouTube Kids")
                        await asyncio.sleep(min(0.5, max(0, deadline - asyncio.get_running_loop().time())))
                        continue
                    if payload.get("ready") not in {"interactive", "complete"}:
                        await asyncio.sleep(min(0.5, max(0, deadline - asyncio.get_running_loop().time())))
                        continue
                    ready_seen = True
                    before = len(collected)
                    for card in payload.get("cards", []):
                        key = str(card.get("href", ""))
                        if key:
                            collected[key] = card
                    stable_rounds = stable_rounds + 1 if len(collected) == before else 0
                    if len(collected) >= _MAX_COLLECTED_CARDS or (collected and stable_rounds >= 3):
                        return list(collected.values())[:_MAX_COLLECTED_CARDS]
                    await asyncio.sleep(min(0.5, max(0, deadline - asyncio.get_running_loop().time())))
                if ready_seen:
                    return list(collected.values())[:_MAX_COLLECTED_CARDS]
                raise RuntimeError("CDP navigation did not reach YouTube Kids")
        except BaseException as exc:
            original_error = exc
            raise
        finally:
            if target_id is not None:
                try:
                    async with websockets.connect(version["webSocketDebuggerUrl"]) as browser:
                        await self._command(browser, 5, "Target.closeTarget", {"targetId": target_id})
                except BaseException:
                    if original_error is None:
                        raise
                    logger.warning("Kids target cleanup failed after ingest error")


async def ingest_once(
    db: Database,
    browser: YouTubeKidsCDP,
    classifier: OpenCodexKidsClassifier,
    *,
    max_cards_per_source: int = 48,
    channel_policy_version: str = "sampled-channel-v1",
    channel_recheck_seconds: int = 604800,
    channel_sample_size: int = 8,
    judge: JudgeService | None = None,
) -> IngestReport:
    report = IngestReport()
    if judge is not None:
        report.blocked += await judge.reconcile_catalog_policy()
    all_sources = await db.catalog_sources_list()
    home_source = next((source for source in all_sources if source.get("reference") == HOME_SOURCE_REFERENCE), None)
    if home_source is None:
        home_source = await db.catalog_create(
            "source",
            {
                "kind": "channel",
                "reference": HOME_SOURCE_REFERENCE,
                "title": "YouTube Kids Home",
                "correlation_id": "kids-home-source",
            },
        )
        home_source = await db.catalog_transition(
            "source",
            int(home_source["id"]),
            {
                "state": "approved",
                "actor": "kids-home-policy",
                "reason": "Use the logged-in YouTube Kids home as the ingest source",
                "correlation_id": "kids-home-approved",
            },
        )
    raw_cards: list[dict[str, Any]] = []
    if home_source.get("state") == "approved":
        try:
            raw_cards = await browser.cards_for_source("channel", HOME_SOURCE_REFERENCE)
            report.cards_seen = len(raw_cards)
        except Exception:
            report.errors += 1
            logger.exception("Kids home ingest failed")

    # Discover channel identities from Home, but leave them candidate-only until a parent approves them.
    known_references = {
        channel_id_from_reference(str(source.get("reference", "")))
        for source in await db.catalog_sources_list()
        if source.get("kind") == "channel"
    }
    for card in raw_cards[:max_cards_per_source]:
        channel_id = str(card.get("channel_id", "")).strip()
        if not _CHANNEL_ID.fullmatch(channel_id) or channel_id in known_references:
            continue
        channel_title = (
            str(card.get("channel_title", "")).strip()
            or str(card.get("label", "")).strip()
        )[:500]
        if judge is not None:
            decision = await judge.match_blocklist(
                video_id="",
                title=str(card.get("title", "")),
                channel_id=channel_id,
                channel_title=channel_title,
                video_url=str(card.get("href", "")),
                video_context=channel_id,
            )
            if decision:
                report.blocked += 1
                continue
        source = await db.catalog_create(
            "source",
            {
                "kind": "channel",
                "reference": channel_id,
                "title": channel_title,
                "correlation_id": f"kids-discovered-channel-{channel_id}",
            },
        )
        known_references.add(channel_id)
        all_sources.append(source)

    sources = [
        source
        for source in await db.catalog_sources_list()
        if source.get("kind") == "channel"
        and source.get("reference") != HOME_SOURCE_REFERENCE
        and source.get("state") in {"approved", "candidate"}
    ]
    report.sources_seen = len(sources) + (1 if home_source.get("state") == "approved" else 0)
    home_cards_by_channel: dict[str, list[dict[str, Any]]] = {}
    for card in raw_cards[:max_cards_per_source]:
        channel_id = str(card.get("channel_id", "")).strip()
        if channel_id:
            home_cards_by_channel.setdefault(channel_id, []).append(card)

    channel_cards_by_id: dict[str, list[dict[str, Any]]] = {}
    safe_source_by_channel: dict[str, dict[str, Any]] = {}
    for source in sources:
        channel_id = channel_id_from_reference(str(source["reference"]))
        if judge is not None:
            decision = await judge.match_blocklist(
                video_id="",
                title=str(source.get("title", "")),
                channel_id=channel_id,
                channel_title=str(source.get("title", "")),
                video_url=str(source.get("reference", "")),
                video_context=str(source.get("reference", "")),
            )
            if decision:
                report.blocked += 1
                await db.catalog_transition(
                    "source",
                    int(source["id"]),
                    {
                        "state": "blocked",
                        "actor": "kids-guardian-blocklist",
                        "reason": decision["reason"],
                        "correlation_id": f"kids-blocklist-source-{source['id']}",
                    },
                )
                continue
        try:
            channel_cards = await browser.cards_for_source("channel", channel_id)
            report.cards_seen += len(channel_cards)
        except Exception:
            channel_cards = []
            if channel_id not in home_cards_by_channel:
                report.errors += 1
                logger.exception("Kids channel ingest failed")
        channel_cards_by_id[channel_id] = channel_cards
        evidence = channel_evidence(
            channel_cards or home_cards_by_channel.get(channel_id, []),
            channel_id=channel_id,
            limit=channel_sample_size,
        )
        verdict = str(source.get("safety_verdict", "UNCERTAIN")).upper()
        reason = str(source.get("safety_reason", "")).strip()
        if not source_safety_is_current(
            source,
            policy_version=channel_policy_version,
            recheck_seconds=channel_recheck_seconds,
        ):
            report.channels_screened += 1
            if not evidence:
                decision = {
                    "verdict": "UNCERTAIN",
                    "reason": "No usable channel video samples were available",
                }
            else:
                metadata = {
                    "kind": "channel",
                    "channel_id": channel_id,
                    "channel_title": str(source.get("title", "")).strip(),
                    "source_reference": str(source["reference"]).strip(),
                    "policy_version": channel_policy_version,
                    "sample_videos": evidence,
                }
                try:
                    decision = await classifier.classify(metadata)
                except KidsClassificationError:
                    decision = {"verdict": "UNCERTAIN", "reason": "OpenCodex unavailable"}
                except Exception:
                    decision = {"verdict": "UNCERTAIN", "reason": "classifier failure"}
            verdict = str(decision.get("verdict", "UNCERTAIN")).upper()
            reason = str(decision.get("reason", ""))[:1000] or "Sampled channel safety classification"
            await db.catalog_source_safety_update(
                int(source["id"]),
                verdict=verdict,
                reason=reason,
                actor="kids-channel-guardian",
                correlation_id=f"kids-channel-classify-{source['id']}",
                policy_version=channel_policy_version,
                evidence=evidence,
            )
        if verdict == "SAFE":
            if source.get("state") == "approved":
                safe_source_by_channel[channel_id] = source
        elif verdict == "UNSAFE":
            report.blocked += 1
            await db.catalog_transition(
                "source",
                int(source["id"]),
                {
                    "state": "blocked",
                    "actor": "kids-channel-guardian",
                    "reason": reason,
                    "correlation_id": f"kids-channel-block-{source['id']}",
                },
            )
        else:
            report.uncertain += 1

    if home_source.get("state") != "approved" or not safe_source_by_channel:
        return report
    candidates: list[KidsVideoCandidate] = []
    for channel_id, source in safe_source_by_channel.items():
        candidates.extend(
            parse_cards(
                home_cards_by_channel.get(channel_id, [])[:max_cards_per_source],
                source_id=int(source["id"]),
                source_reference=channel_id,
                allowed_channel_ids={channel_id},
            )
        )

    for source in sources:
        channel_id = channel_id_from_reference(str(source["reference"]))
        if source.get("state") != "approved" or channel_id not in safe_source_by_channel:
            continue
        candidates.extend(
            parse_cards(
                channel_cards_by_id.get(channel_id, [])[:max_cards_per_source],
                source_id=int(source["id"]),
                source_reference=channel_id,
                allowed_channel_ids={channel_id},
            )
        )

    seen_candidates: set[str] = set()
    for candidate in candidates:
        if candidate.video_id in seen_candidates:
            continue
        seen_candidates.add(candidate.video_id)
        existing = await db.catalog_item_by_video(candidate.video_id)
        if existing and existing.get("state") in {"revoked", "blocked"}:
            report.skipped += 1
            continue
        if existing:
            item = await db.catalog_item_refresh(
                int(existing["id"]),
                title=candidate.title,
                source_id=candidate.source_id,
                channel_id=candidate.channel_id,
                channel_title=candidate.channel_title,
                thumbnail_url=candidate.thumbnail_url,
                duration_seconds=candidate.duration_seconds,
                visual_category=str(existing.get("visual_category", "general")),
                correlation_id=f"kids-refresh-{candidate.video_id}",
                sync_backlog=False,
            )
        else:
            item = await db.catalog_create(
                "item",
                {
                    "video_id": candidate.video_id,
                    "title": candidate.title,
                    "source_id": candidate.source_id,
                    "channel_id": candidate.channel_id,
                    "channel_title": candidate.channel_title,
                    "thumbnail_url": candidate.thumbnail_url,
                    "duration_seconds": candidate.duration_seconds,
                    "visual_category": "general",
                    "correlation_id": f"kids-ingest-{candidate.video_id}",
                },
            )
            report.candidates_created += 1
        if item is None:
            report.errors += 1
            continue
        if judge is not None:
            source_title = str(
                safe_source_by_channel.get(candidate.source_reference, {}).get("title", "")
            ).strip()
            decision = await judge.match_blocklist(
                video_id=candidate.video_id,
                title=candidate.title,
                channel_id=candidate.channel_id,
                channel_title=candidate.channel_title,
                video_url=f"https://www.youtubekids.com/watch?v={candidate.video_id}",
                video_context=f"{source_title} {candidate.source_reference}",
            )
        else:
            decision = None
        if decision:
            transitioned = await db.catalog_transition(
                "item",
                int(item["id"]),
                {
                    "state": "blocked",
                    "actor": "kids-guardian-blocklist",
                    "reason": decision["reason"],
                    "correlation_id": f"kids-blocklist-item-{candidate.video_id}",
                },
                expected_state=str(item.get("state", "candidate")),
            )
            if transitioned is not None:
                report.blocked += 1
            else:
                report.skipped += 1
            continue
        if existing and existing.get("state") == "approved":
            report.skipped += 1
            continue
        await db.catalog_transition(
            "item",
            int(item["id"]),
            {
                "state": "approved",
                "actor": "kids-channel-policy",
                "reason": "Inherited from Guardian-screened approved channel",
                "correlation_id": f"kids-channel-approval-{candidate.video_id}",
            },
        )
        report.approved += 1
    await db.kids_resolve_sync_backlog()
    return report


async def _main() -> None:
    settings = Settings()
    db = Database(settings.db_path)
    await db.init()
    blocklists = BlocklistService(settings)
    await blocklists.reload(db)
    judge = JudgeService(
        db,
        settings,
        WebhookClient(settings.webhook_timeout_seconds),
        blocklists=blocklists,
    )
    classifier = OpenCodexKidsClassifier(
        base_url=settings.opencodex_base_url,
        model=settings.opencodex_model,
    )
    try:
        report = await ingest_once(
            db,
            YouTubeKidsCDP(os.getenv("KIDS_BROWSER_CDP_URL", "http://127.0.0.1:9223")),
            classifier,
            max_cards_per_source=max(1, int(os.getenv("KIDS_INGEST_MAX_CARDS", "96"))),
            channel_policy_version=settings.kids_channel_policy_version,
            channel_recheck_seconds=settings.kids_channel_recheck_seconds,
            channel_sample_size=settings.kids_channel_sample_size,
            judge=judge,
        )
        if report.errors == 0:
            await db.set_setting("kids_ingest_last_success_at", utc_now_iso())
        print(json.dumps(asdict(report), sort_keys=True))
    finally:
        await classifier.close()


if __name__ == "__main__":
    asyncio.run(_main())
