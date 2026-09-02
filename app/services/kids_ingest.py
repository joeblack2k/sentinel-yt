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
_KIDS_HOST = "www.youtubekids.com"
_ACCOUNTS_HOST = "accounts.google.com"
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_DURATION = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")
_CHANNEL_FROM_LABEL = re.compile(r"\sby\s(.+?)(?:\s[\d,]+ views|\s\d+ views|\s*$)", re.IGNORECASE)
_MAX_COLLECTED_CARDS = 160
_MAX_CARDS_PER_DISCOVERY_BUCKET = _MAX_COLLECTED_CARDS // 4
_MAX_SCROLL_STEPS = 12
_SEARCH_TERMS = (
    "animals",
    "dieren",
    "lego",
    "building",
    "bouwen",
    "science",
    "wetenschap",
    "stories",
    "verhalen",
    "learning",
    "leren",
    "nature",
    "natuur",
    "creative",
    "knutselen",
    "space",
    "ruimte",
)
_DUTCH_SEARCH_TERMS = frozenset(
    {
        "dieren",
        "bouwen",
        "wetenschap",
        "verhalen",
        "leren",
        "natuur",
        "knutselen",
        "ruimte",
    }
)


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


class KidsBrowserSetupRequired(RuntimeError):
    """The persistent Kids page still needs one-time parent setup."""


def _duration_seconds(value: str) -> int:
    match = _DURATION.fullmatch((value or "").strip())
    if not match:
        return 0
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)


def source_url(kind: str, reference: str) -> str:
    raw = reference.strip()
    if kind == "channel" and raw == HOME_SOURCE_REFERENCE:
        return f"https://{_KIDS_HOST}/"
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc.lower() != _KIDS_HOST:
            raise ValueError(f"Kids source URLs must stay on {_KIDS_HOST}")
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
        self._target_id: str | None = None

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

    @staticmethod
    def _is_kids_page(target: dict[str, Any]) -> bool:
        parsed = urllib.parse.urlparse(str(target.get("url", "")))
        return parsed.scheme == "https" and parsed.hostname == _KIDS_HOST

    @staticmethod
    def _is_accounts_page(target: dict[str, Any]) -> bool:
        parsed = urllib.parse.urlparse(str(target.get("url", "")))
        return parsed.scheme == "https" and parsed.hostname == _ACCOUNTS_HOST

    @classmethod
    def _validate_target_set(
        cls,
        targets: Any,
        *,
        target_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(targets, list):
            raise RuntimeError("CDP target list is invalid")
        pages = [target for target in targets if target.get("type") == "page"]
        kids_pages = [target for target in pages if cls._is_kids_page(target)]
        allowed_pages = [target for target in pages if cls._is_kids_page(target) or cls._is_accounts_page(target)]
        if not kids_pages:
            raise RuntimeError("No existing YouTube Kids CDP target")
        if len(kids_pages) != 1 or len(allowed_pages) != len(pages):
            raise RuntimeError(
                "CDP target set must contain exactly one YouTube Kids page and optional accounts.google.com pages"
            )
        target = kids_pages[0]
        if not target.get("id") or not target.get("webSocketDebuggerUrl"):
            raise RuntimeError("YouTube Kids CDP page is not connectable")
        if target_id is not None and str(target["id"]) != target_id:
            raise RuntimeError("Persistent YouTube Kids CDP target changed")
        return target

    @staticmethod
    def _is_requested_kids_url(actual: urllib.parse.ParseResult, requested: str) -> bool:
        expected = urllib.parse.urlparse(requested)
        return (
            actual.scheme == "https"
            and actual.hostname == _KIDS_HOST
            and actual.path.rstrip("/") == expected.path.rstrip("/")
            and actual.query == expected.query
        )

    async def _reusable_target(self) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + max(0.5, self.wait_seconds)
        while True:
            targets = await self._json("/json/list")
            try:
                target = self._validate_target_set(targets, target_id=self._target_id)
            except RuntimeError as exc:
                if str(exc) != "No existing YouTube Kids CDP target":
                    raise
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise
                await asyncio.sleep(min(0.2, remaining))
                continue
            if self._target_id is None:
                self._target_id = str(target["id"])
                logger.info("Kids ingest reusing existing YouTube Kids browser page")
            return target

    @staticmethod
    def _search_url(term: str) -> str:
        query = urllib.parse.urlencode({"q": term})
        return f"https://{_KIDS_HOST}/search?{query}"

    @staticmethod
    def _card_key(card: dict[str, Any]) -> str:
        href = str(card.get("href", "")).strip()
        parsed = urllib.parse.urlparse(href)
        video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
        if _VIDEO_ID.fullmatch(video_id):
            return f"video:{video_id}"
        return href

    @staticmethod
    def _card_expression() -> str:
        return """(() => {
          const payload = {
            url: window.location.href,
            ready: document.readyState,
            setup_required: document.title.trim() === "Set up YouTube Kids"
              || (document.body?.innerText || "").toLowerCase().includes(
                "get a parent to set up youtube kids"
              ),
            scroll_y: window.scrollY || 0,
            scroll_height: document.documentElement.scrollHeight || 0,
            cards: Array.from(document.querySelectorAll("ytk-compact-video-renderer")).map(x => {
                const data = x.data || (typeof x.get === "function" ? x.get("data") : {}) || {};
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
            }),
            category_urls: Array.from(document.querySelectorAll("a[href]"))
              .map(x => new URL(x.href, window.location.href))
              .filter(x => x.protocol === "https:" && x.hostname === "www.youtubekids.com"
                && (x.pathname.startsWith("/category/") || x.pathname.startsWith("/categories/")))
              .map(x => x.href)
          };
          const viewport = Math.max(window.innerHeight || 0, 480);
          const max_scroll = Math.max(0, document.documentElement.scrollHeight - viewport);
          window.scrollTo(0, Math.min(max_scroll, (window.scrollY || 0) + Math.floor(viewport * 0.8)));
          return JSON.stringify(payload);
        })()"""

    async def _collect_route(
        self,
        page: Any,
        target_url: str,
        collected: dict[str, dict[str, Any]],
        *,
        category_urls: set[str] | None = None,
        max_cards: int = _MAX_COLLECTED_CARDS,
    ) -> None:
        command_timeout = max(5.0, min(15.0, self.wait_seconds))
        try:
            navigation = await self._command(
                page,
                3,
                "Page.navigate",
                {"url": target_url},
                timeout=command_timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("Kids page navigation command timed out; waiting for requested route")
            navigation = {}
        if navigation.get("errorText"):
            raise RuntimeError("CDP navigation failed")

        expression = self._card_expression()
        stable_rounds = 0
        last_position: tuple[int, int] | None = None
        ready_seen = False
        external_url = ""
        wrong_kids_url = ""
        scroll_steps = 0
        deadline = asyncio.get_running_loop().time() + max(0.1, self.wait_seconds)
        sleep_seconds = min(0.25, max(0.01, self.wait_seconds / 10))
        while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            try:
                result = await self._command(
                    page,
                    4,
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True},
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                break
            payload = json.loads(result["result"].get("value", "{}"))
            if payload.get("setup_required"):
                raise KidsBrowserSetupRequired(
                    "YouTube Kids parent setup is required in the persistent browser"
                )
            actual_url = urllib.parse.urlparse(str(payload.get("url", "")))
            if not self._is_requested_kids_url(actual_url, target_url):
                if payload.get("url") != "about:blank":
                    if actual_url.hostname == _KIDS_HOST:
                        wrong_kids_url = str(payload.get("url", ""))
                    else:
                        external_url = str(payload.get("url", ""))
                await asyncio.sleep(min(sleep_seconds, max(0, deadline - asyncio.get_running_loop().time())))
                continue
            if payload.get("ready") not in {"interactive", "complete"}:
                await asyncio.sleep(min(sleep_seconds, max(0, deadline - asyncio.get_running_loop().time())))
                continue
            ready_seen = True
            if category_urls is not None:
                for value in payload.get("category_urls", []):
                    parsed = urllib.parse.urlparse(str(value))
                    if parsed.scheme == "https" and parsed.hostname == _KIDS_HOST and parsed.path.startswith(
                        ("/category/", "/categories/")
                    ):
                        category_urls.add(urllib.parse.urlunparse(parsed))
            before = len(collected)
            for card in payload.get("cards", []):
                key = self._card_key(card)
                if key and key not in collected:
                    if len(collected) >= max_cards:
                        break
                    collected[key] = card
            position = (
                int(payload.get("scroll_y", 0) or 0),
                int(payload.get("scroll_height", 0) or 0),
            )
            if len(collected) >= max_cards:
                return
            stable_rounds = (
                stable_rounds + 1
                if len(collected) == before and position == last_position
                else 0
            )
            last_position = position
            scroll_steps += 1
            if stable_rounds >= 3 or scroll_steps >= _MAX_SCROLL_STEPS:
                return
            await asyncio.sleep(min(sleep_seconds, max(0, deadline - asyncio.get_running_loop().time())))
        if ready_seen:
            return
        if external_url:
            raise RuntimeError("CDP navigation left YouTube Kids")
        if wrong_kids_url:
            raise RuntimeError("CDP navigation did not reach the requested YouTube Kids page")
        raise RuntimeError("CDP navigation did not reach YouTube Kids")

    async def _restore_home_on_page(self, page: Any) -> None:
        await self._command(
            page,
            6,
            "Page.navigate",
            {"url": f"https://{_KIDS_HOST}/"},
            timeout=max(10.0, self.wait_seconds),
        )

    async def cards_for_source(self, kind: str, reference: str) -> list[dict[str, Any]]:
        target_url = source_url(kind, reference)
        target = await self._reusable_target()
        async with websockets.connect(target["webSocketDebuggerUrl"]) as page:
            try:
                command_timeout = max(5.0, min(15.0, self.wait_seconds))
                try:
                    await self._command(page, 2, "Runtime.enable", timeout=command_timeout)
                except asyncio.TimeoutError:
                    await self._command(page, 20, "Runtime.enable", timeout=command_timeout)
                if kind != "channel" or reference != HOME_SOURCE_REFERENCE:
                    collected: dict[str, dict[str, Any]] = {}
                    await self._collect_route(page, target_url, collected)
                    return list(collected.values())[:_MAX_COLLECTED_CARDS]

                home_cards: dict[str, dict[str, Any]] = {}
                category_urls: set[str] = set()
                await self._collect_route(
                    page,
                    f"https://{_KIDS_HOST}/",
                    home_cards,
                    category_urls=category_urls,
                    max_cards=_MAX_CARDS_PER_DISCOVERY_BUCKET,
                )
                category_cards: dict[str, dict[str, Any]] = {}
                for category_url in sorted(category_urls)[: _MAX_SCROLL_STEPS]:
                    await self._collect_route(
                        page,
                        category_url,
                        category_cards,
                        max_cards=_MAX_CARDS_PER_DISCOVERY_BUCKET,
                    )
                dutch_search_cards: dict[str, dict[str, Any]] = {}
                english_search_cards: dict[str, dict[str, Any]] = {}
                for term in _SEARCH_TERMS:
                    search_cards = (
                        dutch_search_cards if term in _DUTCH_SEARCH_TERMS else english_search_cards
                    )
                    await self._collect_route(
                        page,
                        self._search_url(term),
                        search_cards,
                        max_cards=_MAX_CARDS_PER_DISCOVERY_BUCKET,
                    )
                collected: dict[str, dict[str, Any]] = {}
                for cards in (
                    home_cards,
                    category_cards,
                    dutch_search_cards,
                    english_search_cards,
                ):
                    for key, card in cards.items():
                        collected.setdefault(key, card)
                return list(collected.values())[:_MAX_COLLECTED_CARDS]
            finally:
                try:
                    await self._restore_home_on_page(page)
                except Exception:
                    logger.warning("Could not restore the persistent YouTube Kids page")

    async def youtube_cookies(self) -> list[dict[str, Any]]:
        target = await self._reusable_target()
        async with websockets.connect(target["webSocketDebuggerUrl"]) as page:
            result = await self._command(
                page,
                30,
                "Network.getAllCookies",
                timeout=max(5.0, min(15.0, self.wait_seconds)),
            )
        cookies = result.get("cookies", [])
        return [
            {
                "domain": str(cookie.get("domain", "")),
                "path": str(cookie.get("path", "/")),
                "secure": bool(cookie.get("secure")),
                "expires": int(cookie.get("expires", 0) or 0),
                "name": str(cookie.get("name", "")),
                "value": str(cookie.get("value", "")),
            }
            for cookie in cookies
            if isinstance(cookie, dict)
            and (
                "youtube.com" in str(cookie.get("domain", "")).lower()
                or "google.com" in str(cookie.get("domain", "")).lower()
            )
            and cookie.get("name")
            and cookie.get("value")
        ]

    async def restore_home(self) -> None:
        """Leave the persistent user page on Kids home without creating a target."""
        if self._target_id is None:
            return
        try:
            target = await self._target(self._target_id)
            async with websockets.connect(target["webSocketDebuggerUrl"]) as page:
                await self._restore_home_on_page(page)
        except Exception:
            logger.warning("Could not restore the persistent YouTube Kids page")


async def ingest_once(
    db: Database,
    browser: YouTubeKidsCDP,
    classifier: OpenCodexKidsClassifier,
    *,
    max_cards_per_source: int = 48,
    channel_policy_version: str = "sampled-channel-v1",
    channel_recheck_seconds: int = 604800,
    channel_sample_size: int = 8,
    source_batch_size: int | None = None,
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
        except KidsBrowserSetupRequired:
            report.errors += 1
            logger.warning("Kids ingest paused: complete parent setup in the persistent browser")
            return report
        except Exception:
            report.errors += 1
            logger.exception("Kids home ingest failed")
            return report

    # Discover channel identities from Home; SAFE sources are auto-approved and remain parent-revocable.
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

    eligible_sources = [
        source
        for source in await db.catalog_sources_list()
        if source.get("kind") == "channel"
        and source.get("reference") != HOME_SOURCE_REFERENCE
        and source.get("state") in {"approved", "candidate"}
    ]
    report.sources_seen = len(eligible_sources) + (
        1 if home_source.get("state") == "approved" else 0
    )
    sources = eligible_sources
    next_source_offset: int | None = None
    if source_batch_size is not None and source_batch_size > 0 and len(sources) > source_batch_size:
        try:
            source_offset = max(0, int((await db.get_setting("kids_ingest_source_offset")) or "0"))
        except (TypeError, ValueError):
            source_offset = 0
        source_offset %= len(sources)
        ordered_sources = sources[source_offset:] + sources[:source_offset]
        sources = ordered_sources[:source_batch_size]
        next_source_offset = (source_offset + len(sources)) % len(eligible_sources)
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
        except KidsBrowserSetupRequired:
            report.errors += 1
            logger.warning("Kids ingest paused: complete parent setup in the persistent browser")
            return report
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
                    decision = {
                        "verdict": "UNCERTAIN",
                        "language": "unknown",
                        "reason": "OpenCodex unavailable",
                    }
                except Exception:
                    decision = {
                        "verdict": "UNCERTAIN",
                        "language": "unknown",
                        "reason": "classifier failure",
                    }
            verdict = str(decision.get("verdict", "UNCERTAIN")).upper()
            if verdict not in {"SAFE", "UNSAFE", "UNCERTAIN"}:
                verdict = "UNCERTAIN"
            language = str(decision.get("language", "unknown")).lower()
            if language not in {"nl", "en", "mixed", "unknown"}:
                language = "unknown"
            reason = str(decision.get("reason", ""))[:1000] or "Sampled channel safety classification"
            updated_source = await db.catalog_source_safety_update(
                int(source["id"]),
                verdict=verdict,
                language=language,
                reason=reason,
                actor="kids-channel-guardian",
                correlation_id=f"kids-channel-classify-{source['id']}",
                policy_version=channel_policy_version,
                evidence=evidence,
            )
            if updated_source is not None:
                source = updated_source
        if verdict == "SAFE":
            if source.get("state") == "candidate":
                approved_source = await db.catalog_transition(
                    "source",
                    int(source["id"]),
                    {
                        "state": "approved",
                        "actor": "kids-channel-guardian",
                        "reason": "Automatically approved after SAFE channel classification",
                        "correlation_id": f"kids-channel-approval-{source['id']}",
                    },
                    expected_state="candidate",
                )
                if approved_source is None:
                    report.errors += 1
                    continue
                source = approved_source
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
        if next_source_offset is not None:
            await db.set_setting("kids_ingest_source_offset", str(next_source_offset))
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
        approved_source = safe_source_by_channel.get(channel_id)
        if approved_source is None:
            continue
        candidates.extend(
            parse_cards(
                channel_cards_by_id.get(channel_id, [])[:max_cards_per_source],
                source_id=int(approved_source["id"]),
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
    if next_source_offset is not None:
        await db.set_setting("kids_ingest_source_offset", str(next_source_offset))
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
    browser = YouTubeKidsCDP(os.getenv("KIDS_BROWSER_CDP_URL", "http://127.0.0.1:9223"))
    try:
        report = await ingest_once(
            db,
            browser,
            classifier,
            max_cards_per_source=max(1, int(os.getenv("KIDS_INGEST_MAX_CARDS", "96"))),
            channel_policy_version=settings.kids_channel_policy_version,
            channel_recheck_seconds=settings.kids_channel_recheck_seconds,
            channel_sample_size=settings.kids_channel_sample_size,
            source_batch_size=max(1, int(os.getenv("KIDS_INGEST_SOURCE_BATCH_SIZE", "12"))),
            judge=judge,
        )
        if report.errors == 0:
            await db.set_setting("kids_ingest_last_success_at", utc_now_iso())
        print(json.dumps(asdict(report), sort_keys=True))
    finally:
        await browser.restore_home()
        await classifier.close()


if __name__ == "__main__":
    asyncio.run(_main())
