from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from ..config import POLICY_PRESETS
from ..db import KIDS_HOME_SOURCE_REFERENCE, Database
from .blocklists import BlocklistService


_POLICY_KEYS = [p["key"] for p in POLICY_PRESETS]
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


class JudgeService:
    def __init__(self, db: Database, blocklists: BlocklistService | None = None):
        self.db = db
        self.blocklists = blocklists

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
