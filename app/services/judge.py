from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from google import genai
from google.genai import types

from ..config import (
    ALLOW_POLICY_PRESETS,
    DEFAULT_SAFE_PROMPT,
    DEFAULT_WHITELIST_PROMPT,
    OUTPUT_CONTRACT_SUFFIX,
    POLICY_PRESETS,
    Settings,
)
from ..db import KIDS_HOME_SOURCE_REFERENCE, Database, utc_now_iso
from .blocklists import BlocklistService
from .webhook import WebhookClient


class GeminiFatalError(RuntimeError):
    pass


class GeminiOutputError(RuntimeError):
    pass


_POLICY_KEYS = [p["key"] for p in POLICY_PRESETS]
_POLICY_LABELS = {p["key"]: p["label"] for p in POLICY_PRESETS}
_POLICY_KEYWORDS = {
    "block_cocomelon": [
        "cocomelon",
        "coco melon",
        "jj and friends",
        "cocomelon nederlands",
        "cocomelon songs for kids",
    ],
    "block_nursery_factory": [
        "nursery rhymes",
        "kids songs",
        "for toddlers",
        "baby songs",
        "baby anna",
        "zoki nursery",
        "bebe zoki",
        "wheels on the bus",
    ],
    "block_kids_clickbait_animals": [
        "monkey baby",
        "baby monkey",
        "bon bon",
        "animal ht",
        "toilet",
        "poop",
        "potty",
        "ducklings in the swimming pool",
    ],
    "block_skibidi": ["skibidi", "skibidi toilet"],
    "block_huggy_wuggy": ["huggy wuggy", "poppy playtime"],
    "block_rainbow_friends": ["rainbow friends"],
    "block_siren_momo": ["siren head", "momo"],
    "block_prank": ["prank"],
    "block_challenge": ["challenge", "24 hour challenge", "24h challenge"],
    "block_granny": ["granny"],
    "block_fnaf": ["fnaf", "five nights at freddy", "five nights at freddy's"],
    "block_unboxing_eggs": ["unboxing", "surprise egg", "surprise eggs"],
    "block_kill_die": [" kill ", "killing", " die ", "dies", "died"],
    "block_blood_gore_horror": ["blood", "bloed", "gore", "horror"],
    "block_guns_weapons": ["gun", "shoot", "weapon", "wapen", "firearm"],
    "block_elsagate_pregnant": ["pregnant", "zwanger"],
    "block_elsagate_injection": ["injection", "spuit", "doctor", "needle", "surgery"],
    "block_suicide": ["suicide", "zelfmoord", "self harm", "self-harm"],
}
_ALLOW_POLICY_KEYS = [p["key"] for p in ALLOW_POLICY_PRESETS]
_ALLOW_POLICY_LABELS = {p["key"]: p["label"] for p in ALLOW_POLICY_PRESETS}
_ALLOW_POLICY_DEFAULTS = {
    "allow_90s_cartoons": True,
    "allow_00s_cartoons": True,
    "allow_disney_family": True,
    "allow_educational": True,
}
_ALLOW_POLICY_KEYWORDS = {
    "allow_90s_cartoons": ["90s cartoon", "1990s cartoon", "rugrats", "hey arnold", "animaniacs"],
    "allow_00s_cartoons": ["2000s cartoon", "00s cartoon", "kim possible", "fairly oddparents", "avatar"],
    "allow_all_cartoons": ["cartoon", "animation", "animated", "wb kids", "cartoon network"],
    "allow_disney_family": ["disney", "disney jr", "pixar", "mickey", "minnie", "spidey and his amazing friends"],
    "allow_educational": ["educational", "learn", "science", "math", "reading", "school", "kids academy"],
    "allow_religion": ["bible", "church", "faith", "christian kids", "quran", "torah", "sunday school"],
    "allow_pbs_kids": ["pbs kids", "sesame street", "arthur", "magic school bus", "reading rainbow"],
    "allow_nickelodeon_90s": ["nickelodeon", "rugrats", "doug", "ren and stimpy", "catdog"],
    "allow_cartoon_network_classics": ["dexter's laboratory", "powerpuff girls", "johnny bravo", "ed edd n eddy"],
    "allow_disney_afternoon": ["ducktales", "darkwing duck", "talespin", "goof troop"],
    "allow_animal_documentaries": ["animal documentary", "wildlife", "national geographic kids", "nat geo kids"],
    "allow_nature_science": ["space", "planet", "solar system", "nature", "experiment", "science for kids"],
    "allow_music_rhythm": ["music for kids", "rhythm", "sing-along", "children's choir"],
    "allow_arts_crafts": ["arts and crafts", "drawing for kids", "origami", "craft tutorial"],
    "allow_storytelling_books": ["story time", "read aloud", "storybook", "bedtime story"],
    "allow_family_game_shows": ["family quiz", "kids game show", "trivia for kids", "family challenge"],
}
def match_policy_keywords(
    flags: dict[str, bool],
    *,
    title: str = "",
    channel_title: str = "",
    video_url: str = "",
    video_context: str = "",
) -> str | None:
    values = (
        str(value or "").casefold()
        for value in (title, channel_title, video_url, video_context)
    )
    hay = " ".join(values)
    compact_hay = re.sub(r"[^a-z0-9]+", "", hay)
    for key in _POLICY_KEYS:
        if not flags.get(key, False):
            continue
        for needle in _POLICY_KEYWORDS.get(key, []):
            normalized_needle = needle.casefold()
            if normalized_needle in hay or re.sub(r"[^a-z0-9]+", "", normalized_needle) in compact_hay:
                return key
    return None


def policy_block_reason(policy_key: str) -> str:
    return f"guardian_policy_{policy_key}"


def _channel_id_from_reference(reference: object) -> str:
    raw = str(reference or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme or parsed.netloc:
        parts = [part for part in parsed.path.split("/") if part]
        return parts[1] if len(parts) == 2 and parts[0] == "channel" else ""
    return raw


def normalize_policy_flags(raw: dict[str, Any] | str | None) -> dict[str, bool]:
    data: dict[str, Any] = {}
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            try:
                loaded = json.loads(text)
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
    elif isinstance(raw, dict):
        data = raw

    return {key: bool(data.get(key, False)) for key in _POLICY_KEYS}


def normalize_allow_policy_flags(raw: dict[str, Any] | str | None) -> dict[str, bool]:
    data: dict[str, Any] = {}
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            try:
                loaded = json.loads(text)
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
    elif isinstance(raw, dict):
        data = raw
    return {key: bool(data.get(key, _ALLOW_POLICY_DEFAULTS.get(key, False))) for key in _ALLOW_POLICY_KEYS}


def build_policy_prompt_addon(flags: dict[str, bool]) -> str:
    enabled = [p for p in POLICY_PRESETS if flags.get(p["key"], False)]
    if not enabled:
        return ""
    lines = [
        "Strict policy overrides enabled by admin toggles:",
        "If a toggle matches the video context, return BLOCK even when content is popular.",
    ]
    for preset in enabled:
        lines.append(f'- {preset["label"]}: {preset["prompt_addon"]}')
    return "\n".join(lines)


def build_allow_policy_prompt_addon(flags: dict[str, bool]) -> str:
    enabled = [p for p in ALLOW_POLICY_PRESETS if flags.get(p["key"], False)]
    if not enabled:
        return "No allow profile categories are enabled. Default to BLOCK."
    lines = [
        "Allow profile categories enabled by admin toggles:",
        "Only ALLOW when the video clearly belongs to these categories.",
    ]
    for preset in enabled:
        lines.append(f'- {preset["label"]}: {preset["prompt_addon"]}')
    return "\n".join(lines)


class JudgeService:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        webhook_client: WebhookClient,
        blocklists: BlocklistService | None = None,
        allowlists: BlocklistService | None = None,
    ):
        self.db = db
        self.settings = settings
        self.webhook_client = webhook_client
        self.blocklists = blocklists
        self.allowlists = allowlists

    async def get_effective_gemini_key(self) -> str:
        runtime = (await self.db.get_setting("gemini_api_key_runtime")) or ""
        if runtime.strip():
            return runtime.strip()
        return (self.settings.gemini_api_key or "").strip()

    async def match_blocklist(
        self,
        *,
        video_id: str,
        title: str,
        channel_id: str,
        channel_title: str,
        video_url: str,
        video_context: str = "",
    ) -> dict[str, Any] | None:
        """Apply exactly the blocking controls exposed on the Guardian blocklist page."""
        blacklist_match = await self.db.find_rule_match(
            video_id,
            channel_id,
            preferred_rule_type="blacklist",
        )
        if blacklist_match:
            return {
                "verdict": "BLOCK",
                "reason": f"Blocked by local blacklist ({blacklist_match['scope']})",
                "confidence": 100,
                "source": "blacklist",
            }

        if self.blocklists is not None:
            file_block = await self.blocklists.match(video_id=video_id, channel_id=channel_id)
            if file_block:
                return {
                    "verdict": "BLOCK",
                    "reason": f"Blocked by file blocklist ({file_block['scope']})",
                    "confidence": 100,
                    "source": "file_blacklist",
                }

        policy_key = await self.match_policy_key(
            title=title,
            channel_title=channel_title,
            video_url=video_url,
            video_context=video_context,
        )
        if policy_key:
            return {
                "verdict": "BLOCK",
                "reason": policy_block_reason(policy_key),
                "confidence": 100,
                "source": "policy",
            }
        return None

    async def match_catalog_source_blocklist(self, source: dict[str, Any]) -> dict[str, Any] | None:
        reference = str(source.get("reference", "")).strip()
        return await self.match_blocklist(
            video_id="",
            title=str(source.get("title", "")),
            channel_id=_channel_id_from_reference(reference),
            channel_title=str(source.get("title", "")),
            video_url=reference,
            video_context=reference,
        )

    async def match_catalog_item_blocklist(
        self,
        item: dict[str, Any],
        source: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        source = source or {}
        source_reference = str(
            item.get("source_reference") or source.get("reference") or ""
        ).strip()
        source_title = str(
            item.get("source_title") or source.get("title") or ""
        ).strip()
        return await self.match_blocklist(
            video_id=str(item.get("video_id", "")),
            title=str(item.get("title", "")),
            channel_id=(
                str(item.get("channel_id", "")).strip()
                or _channel_id_from_reference(source_reference)
            ),
            channel_title=str(item.get("channel_title", "")),
            video_url=f"https://www.youtubekids.com/watch?v={item.get('video_id', '')}",
            video_context=f"{source_title} {source_reference}",
        )

    async def evaluate(
        self,
        *,
        video_id: str,
        title: str,
        channel_id: str,
        channel_title: str,
        video_url: str,
        video_context: str = "",
        enforcement_mode: str = "blocklist",
    ) -> dict[str, Any]:
        mode = "whitelist" if enforcement_mode == "whitelist" else "blocklist"
        cache_key = f"{mode}:{video_id}"

        blocklist_match = await self.match_blocklist(
            video_id=video_id,
            title=title,
            channel_id=channel_id,
            channel_title=channel_title,
            video_url=video_url,
            video_context=video_context,
        )
        if blocklist_match:
            return blocklist_match

        # Whitelist-only mode: only explicit allowlist/profile match should pass.
        if mode == "whitelist":
            whitelist_match = await self.db.find_rule_match(video_id, channel_id, preferred_rule_type="whitelist")
            if whitelist_match:
                return {
                    "verdict": "ALLOW",
                    "reason": f"Allowed by local whitelist ({whitelist_match['scope']})",
                    "confidence": 100,
                    "source": "whitelist",
                }
            if self.allowlists is not None:
                file_allow = await self.allowlists.match(video_id=video_id, channel_id=channel_id)
                if file_allow:
                    return {
                        "verdict": "ALLOW",
                        "reason": f"Allowed by file whitelist ({file_allow['scope']})",
                        "confidence": 100,
                        "source": "file_whitelist",
                    }

            allow_policy_hit = await self._match_allow_policy(
                title=title,
                channel_title=channel_title,
                video_url=video_url,
            )
            if allow_policy_hit:
                return {
                    "verdict": "ALLOW",
                    "reason": f'Allowed by whitelist policy toggle "{allow_policy_hit}"',
                    "confidence": 100,
                    "source": "policy_allowlist",
                }

            cached = await self.db.cache_get(cache_key)
            if cached:
                cached["source"] = cached.get("source", "gemini")
                gated = self._apply_strict_allow_gate(
                    decision=cached,
                    title=title,
                    channel_title=channel_title,
                    video_url=video_url,
                )
                if gated.get("verdict") == "ALLOW":
                    return gated
                return {
                    "verdict": "BLOCK",
                    "reason": gated.get("reason", "Not in active allow profile."),
                    "confidence": 100,
                    "source": gated.get("source", "policy"),
                }

            gemini_enabled = ((await self.db.get_setting("gemini_enabled")) or "true").strip().lower() == "true"
            if not gemini_enabled:
                return {
                    "verdict": "BLOCK",
                    "reason": "Whitelist mode: Gemini is disabled and no allowlist match was found.",
                    "confidence": 100,
                    "source": "policy",
                }

            prompt = await self._effective_whitelist_prompt()
            key = await self.get_effective_gemini_key()
            if not key:
                raise GeminiFatalError("missing_gemini_key")

            payload = {
                "video_id": video_id,
                "video_url": video_url,
                "title": title,
                "channel_id": channel_id,
                "channel_title": channel_title,
            }
            try:
                first = await self._call_gemini(api_key=key, system_prompt=prompt, payload=payload)
                parsed = self._parse_output(first)
            except GeminiOutputError:
                repair_prompt = prompt + "\nReturn strict valid JSON exactly as requested."
                second = await self._call_gemini(api_key=key, system_prompt=repair_prompt, payload=payload)
                parsed = self._parse_output(second)

            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self.settings.decision_cache_ttl_seconds)).isoformat()
            await self.db.cache_set(cache_key, parsed, expires_at)
            gated = self._apply_strict_allow_gate(
                decision=parsed,
                title=title,
                channel_title=channel_title,
                video_url=video_url,
            )
            if gated.get("verdict") == "ALLOW":
                return gated
            return {
                "verdict": "BLOCK",
                "reason": gated.get("reason", "Whitelist mode: not in active allow profile."),
                "confidence": 100,
                "source": gated.get("source", "policy"),
            }

        # Blocklist mode: only blocklist path is enforced. Non-blocked content can proceed.

        cached = await self.db.cache_get(cache_key)
        if cached:
            cached["source"] = cached.get("source", "gemini")
            return self._apply_strict_allow_gate(
                decision=cached,
                title=title,
                channel_title=channel_title,
                video_url=video_url,
            )

        gemini_enabled = ((await self.db.get_setting("gemini_enabled")) or "true").strip().lower() == "true"
        if not gemini_enabled:
            return {
                "verdict": "ALLOW",
                "reason": "Gemini is disabled. Only local rules and blocklists are enforced.",
                "confidence": 0,
                "source": "fallback",
            }

        prompt = await self._effective_prompt()
        key = await self.get_effective_gemini_key()
        if not key:
            raise GeminiFatalError("missing_gemini_key")

        payload = {
            "video_id": video_id,
            "video_url": video_url,
            "title": title,
            "channel_id": channel_id,
            "channel_title": channel_title,
        }

        try:
            first = await self._call_gemini(api_key=key, system_prompt=prompt, payload=payload)
            parsed = self._parse_output(first)
        except GeminiOutputError:
            # one strict repair attempt
            repair_prompt = prompt + "\nReturn strict valid JSON exactly as requested."
            second = await self._call_gemini(api_key=key, system_prompt=repair_prompt, payload=payload)
            parsed = self._parse_output(second)

        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self.settings.decision_cache_ttl_seconds)).isoformat()
        await self.db.cache_set(cache_key, parsed, expires_at)
        return self._apply_strict_allow_gate(
            decision=parsed,
            title=title,
            channel_title=channel_title,
            video_url=video_url,
        )

    async def match_policy_key(
        self,
        *,
        title: str,
        channel_title: str,
        video_url: str,
        video_context: str = "",
    ) -> str | None:
        policy_flags_raw = (await self.db.get_setting("policy_flags_json")) or "{}"
        return match_policy_keywords(
            normalize_policy_flags(policy_flags_raw),
            title=title,
            channel_title=channel_title,
            video_url=video_url,
            video_context=video_context,
        )

    async def match_policy_override(
        self,
        *,
        title: str,
        channel_title: str,
        video_url: str,
        video_context: str = "",
    ) -> str | None:
        policy_key = await self.match_policy_key(
            title=title,
            channel_title=channel_title,
            video_url=video_url,
            video_context=video_context,
        )
        return _POLICY_LABELS.get(policy_key, policy_key) if policy_key else None

    async def reconcile_catalog_policy(self) -> int:
        """Reconcile catalog rows against the live Guardian blocklist controls."""
        blocked = 0
        blacklist_rules = await self.db.list_rules(limit=None, rule_type="blacklist")
        blacklist_values = {
            "video": {
                str(rule["value"]).strip()
                for rule in blacklist_rules
                if rule.get("scope") == "video"
            },
            "channel": {
                str(rule["value"]).strip()
                for rule in blacklist_rules
                if rule.get("scope") == "channel"
            },
        }
        policy_flags = normalize_policy_flags(
            (await self.db.get_setting("policy_flags_json")) or "{}"
        )

        async def match_snapshot(
            *,
            video_id: str,
            title: str,
            channel_id: str,
            channel_title: str,
            video_url: str,
            video_context: str = "",
        ) -> dict[str, Any] | None:
            blacklist_scope = (
                "video"
                if video_id and video_id in blacklist_values["video"]
                else "channel"
                if channel_id and channel_id in blacklist_values["channel"]
                else ""
            )
            if blacklist_scope:
                return {
                    "verdict": "BLOCK",
                    "reason": f"Blocked by local blacklist ({blacklist_scope})",
                    "confidence": 100,
                    "source": "blacklist",
                }

            if self.blocklists is not None:
                file_block = await self.blocklists.match(
                    video_id=video_id,
                    channel_id=channel_id,
                )
                if file_block:
                    return {
                        "verdict": "BLOCK",
                        "reason": f"Blocked by file blocklist ({file_block['scope']})",
                        "confidence": 100,
                        "source": "file_blacklist",
                    }

            policy_key = match_policy_keywords(
                policy_flags,
                title=title,
                channel_title=channel_title,
                video_url=video_url,
                video_context=video_context,
            )
            if policy_key:
                return {
                    "verdict": "BLOCK",
                    "reason": policy_block_reason(policy_key),
                    "confidence": 100,
                    "source": "policy",
                }
            return None

        async def match_source_snapshot(source: dict[str, Any]) -> dict[str, Any] | None:
            reference = str(source.get("reference", "")).strip()
            return await match_snapshot(
                video_id="",
                title=str(source.get("title", "")),
                channel_id=_channel_id_from_reference(reference),
                channel_title=str(source.get("title", "")),
                video_url=reference,
                video_context=reference,
            )

        async def match_item_snapshot(
            item: dict[str, Any],
            source: dict[str, Any] | None = None,
        ) -> dict[str, Any] | None:
            source = source or {}
            source_reference = str(
                item.get("source_reference") or source.get("reference") or ""
            ).strip()
            source_title = str(
                item.get("source_title") or source.get("title") or ""
            ).strip()
            video_id = str(item.get("video_id", ""))
            return await match_snapshot(
                video_id=video_id,
                title=str(item.get("title", "")),
                channel_id=(
                    str(item.get("channel_id", "")).strip()
                    or _channel_id_from_reference(source_reference)
                ),
                channel_title=str(item.get("channel_title", "")),
                video_url=f"https://www.youtubekids.com/watch?v={video_id}",
                video_context=f"{source_title} {source_reference}",
            )

        for source in await self.db.catalog_sources_list():
            if source.get("reference") == KIDS_HOME_SOURCE_REFERENCE:
                continue
            decision = await match_source_snapshot(source)
            if decision and source.get("state") not in {"blocked", "revoked"}:
                transitioned = await self.db.catalog_transition(
                    "source",
                    int(source["id"]),
                    {
                        "state": "blocked",
                        "actor": "kids-guardian-blocklist",
                        "reason": decision["reason"],
                        "correlation_id": f"kids-blocklist-source-{source['id']}",
                    },
                )
                if transitioned is not None:
                    blocked += 1

        for item in await self.db.catalog_approved_items_for_policy():
            decision = await match_item_snapshot(item)
            if not decision:
                continue
            transitioned = await self.db.catalog_transition(
                "item",
                int(item["id"]),
                {
                    "state": "blocked",
                    "actor": "kids-guardian-blocklist",
                    "reason": decision["reason"],
                    "correlation_id": f"kids-blocklist-item-{item['video_id']}",
                },
                expected_state="approved",
            )
            if transitioned is not None:
                blocked += 1

        for row in await self.db.catalog_blocklist_restore_candidates():
            entity_type = str(row["entity_type"])
            entity_id = int(row["entity_id"])
            current = await self.db.catalog_get(entity_type, entity_id)
            if current is None:
                continue
            if entity_type == "source":
                decision = await match_source_snapshot(current)
            else:
                source = (
                    await self.db.catalog_get("source", int(current["source_id"]))
                    if current.get("source_id") is not None
                    else None
                )
                decision = await match_item_snapshot(current, source)
            if decision:
                continue
            await self.db.catalog_transition(
                entity_type,
                entity_id,
                {
                    "state": row["previous_state"],
                    "actor": "kids-guardian-blocklist-reconcile",
                    "reason": "Blocklist no longer matches; restored previous catalog state",
                    "correlation_id": f"kids-blocklist-restore-{entity_type}-{entity_id}",
                },
                expected_state="blocked",
            )
        return blocked

    async def _effective_prompt(self) -> str:
        custom = (await self.db.get_setting("custom_prompt")) or ""
        base = custom.strip() or DEFAULT_SAFE_PROMPT
        policy_flags_raw = (await self.db.get_setting("policy_flags_json")) or "{}"
        policy_flags = normalize_policy_flags(policy_flags_raw)
        addon = build_policy_prompt_addon(policy_flags)
        if addon:
            base = f"{base}\n\n{addon}"
        return f"{base}{OUTPUT_CONTRACT_SUFFIX}"

    async def _effective_whitelist_prompt(self) -> str:
        custom = (await self.db.get_setting("custom_prompt")) or ""
        base = custom.strip() or DEFAULT_WHITELIST_PROMPT
        allow_flags_raw = (await self.db.get_setting("allow_policy_flags_json")) or "{}"
        allow_flags = normalize_allow_policy_flags(allow_flags_raw)
        addon = build_allow_policy_prompt_addon(allow_flags)
        return f"{base}\n\n{addon}{OUTPUT_CONTRACT_SUFFIX}"

    async def get_effective_prompt_preview(self) -> str:
        return await self._effective_prompt()

    async def get_effective_whitelist_prompt_preview(self) -> str:
        return await self._effective_whitelist_prompt()

    async def _match_policy_override(
        self,
        *,
        title: str,
        channel_title: str,
        video_url: str,
        video_context: str = "",
    ) -> str | None:
        return await self.match_policy_override(
            title=title,
            channel_title=channel_title,
            video_url=video_url,
            video_context=video_context,
        )

    async def _match_allow_policy(self, *, title: str, channel_title: str, video_url: str) -> str | None:
        allow_flags_raw = (await self.db.get_setting("allow_policy_flags_json")) or "{}"
        flags = normalize_allow_policy_flags(allow_flags_raw)
        hay = f" {title} {channel_title} {video_url} ".lower()
        for key, enabled in flags.items():
            if not enabled:
                continue
            for needle in _ALLOW_POLICY_KEYWORDS.get(key, []):
                if needle in hay:
                    return _ALLOW_POLICY_LABELS.get(key, key)
        return None

    async def _call_gemini(self, *, api_key: str, system_prompt: str, payload: dict[str, Any]) -> str:
        def _run() -> str:
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=self.settings.gemini_model,
                contents=[
                    {
                        "role": "user",
                        "parts": [
                            {
                                "text": (
                                    "Analyze this YouTube video for a 6-year-old safety policy.\n"
                                    f"Video URL: {payload['video_url']}\n"
                                    f"Video ID: {payload['video_id']}\n"
                                    f"Title: {payload['title']}\n"
                                    f"Channel ID: {payload['channel_id']}\n"
                                    f"Channel title: {payload['channel_title']}"
                                )
                            }
                        ],
                    }
                ],
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_json_schema={
                        "type": "object",
                        "required": ["verdict", "reason", "confidence"],
                        "properties": {
                            "verdict": {"type": "string", "enum": ["ALLOW", "BLOCK"]},
                            "reason": {"type": "string"},
                            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                        },
                    },
                ),
            )
            return (response.text or "").strip()

        try:
            return await asyncio.to_thread(_run)
        except Exception as err:
            message = str(err)
            if self._is_fatal_auth_or_quota(message):
                raise GeminiFatalError(message) from err
            raise GeminiOutputError(message) from err

    def _apply_strict_allow_gate(
        self,
        *,
        decision: dict[str, Any],
        title: str,
        channel_title: str,
        video_url: str,
    ) -> dict[str, Any]:
        if str(decision.get("verdict", "")).upper() != "ALLOW":
            return decision

        confidence = int(decision.get("confidence", 0) or 0)
        min_conf = max(0, min(100, int(self.settings.strict_allow_min_confidence)))
        if confidence < min_conf:
            return {
                "verdict": "BLOCK",
                "reason": f"Strict nanny mode: ALLOW confidence {confidence} is below minimum {min_conf}.",
                "confidence": 100,
                "source": "policy",
            }

        return decision

    @staticmethod
    def _is_fatal_auth_or_quota(msg: str) -> bool:
        check = msg.lower()
        needles = [
            "401",
            "403",
            "429",
            "quota",
            "api key",
            "permission",
            "invalid argument",
            "unauthenticated",
            "api_key_invalid",
            "billing",
        ]
        return any(n in check for n in needles)

    def _parse_output(self, raw: str) -> dict[str, Any]:
        text = raw.strip()
        if not text:
            raise GeminiOutputError("empty_output")

        # robust extraction if model wraps JSON with text
        if not text.startswith("{"):
            match = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not match:
                raise GeminiOutputError("json_not_found")
            text = match.group(0)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise GeminiOutputError("json_decode_error") from err

        verdict = data.get("verdict")
        reason = str(data.get("reason", "")).strip()
        confidence = data.get("confidence")

        if verdict not in {"ALLOW", "BLOCK"}:
            raise GeminiOutputError("invalid_verdict")
        if not reason:
            reason = "No reason provided"

        try:
            confidence = int(confidence)
        except Exception as err:
            raise GeminiOutputError("invalid_confidence") from err

        confidence = max(0, min(100, confidence))
        return {
            "verdict": verdict,
            "reason": reason,
            "confidence": confidence,
            "source": "gemini",
        }

    async def handle_fatal_failure(self, err: Exception) -> None:
        message = str(err)
        await self.db.set_setting("judge_ok", "false")
        await self.db.set_setting("last_error", message)

        now = datetime.now(timezone.utc)
        last_sent_raw = (await self.db.get_setting("last_failure_alert_at")) or ""
        should_alert = True
        if last_sent_raw:
            try:
                last_dt = datetime.fromisoformat(last_sent_raw)
                if now - last_dt < timedelta(minutes=5):
                    should_alert = False
            except Exception:
                should_alert = True

        if should_alert:
            hook = (await self.db.get_setting("failure_webhook_url")) or ""
            if hook:
                await self.webhook_client.post_json(
                    hook,
                    {
                        "event": "sentinel_gemini_failure_degraded",
                        "active": (await self.db.get_setting("active") or "true") == "true",
                        "judge_ok": False,
                        "error": message,
                        "timestamp": utc_now_iso(),
                    },
                )
            await self.db.set_setting("last_failure_alert_at", now.isoformat())
