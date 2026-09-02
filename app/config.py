from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _kids_resolver_min_quality_height() -> int:
    try:
        value = int(os.getenv("KIDS_RESOLVER_MIN_QUALITY_HEIGHT", "720"))
    except (TypeError, ValueError):
        return 720
    return value if 720 <= value <= 1080 else 720


@dataclass(frozen=True)
class Settings:
    app_name: str = "Sentinel"
    build_version: str = field(default_factory=lambda: os.getenv("SENTINEL_BUILD_VERSION", "v1"))
    host: str = field(default_factory=lambda: os.getenv("SENTINEL_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("SENTINEL_PORT", "8090")))
    data_dir: str = field(default_factory=lambda: os.getenv("SENTINEL_DATA_DIR", "/data"))
    db_path: str = field(default_factory=lambda: os.getenv("SENTINEL_DB_PATH", "/data/sentinel.db"))
    schedule_timezone_default: str = field(default_factory=lambda: os.getenv("SENTINEL_TIMEZONE_DEFAULT", ""))
    webhook_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("SENTINEL_WEBHOOK_TIMEOUT_SECONDS", "8"))
    )
    remote_blocklists_cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("SENTINEL_REMOTE_BLOCKLISTS_CACHE_TTL_SECONDS", "900"))
    )
    opencodex_base_url: str = field(
        default_factory=lambda: os.getenv("OPENCODEX_BASE_URL", "http://192.168.2.93:10100/v1").rstrip("/")
    )
    opencodex_model: str = field(
        default_factory=lambda: os.getenv("OPENCODEX_MODEL", "google-antigravity/gemini-3.7-flash")
    )
    kids_ingest_freshness_seconds: int = field(
        default_factory=lambda: int(os.getenv("KIDS_INGEST_FRESHNESS_SECONDS", "1800"))
    )
    kids_resolver_cdp_url: str = field(
        default_factory=lambda: os.getenv("KIDS_BROWSER_CDP_URL", "http://127.0.0.1:9223").rstrip("/")
    )
    kids_resolver_batch_size: int = field(default_factory=lambda: int(os.getenv("KIDS_RESOLVER_BATCH_SIZE", "2")))
    kids_resolver_timeout_seconds: int = field(
        default_factory=lambda: max(5, min(120, int(os.getenv("KIDS_RESOLVER_TIMEOUT_SECONDS", "35"))))
    )
    kids_resolver_js_runtime: str = field(
        default_factory=lambda: os.getenv("KIDS_YTDLP_JS_RUNTIME", "node").strip() or "node"
    )
    kids_resolve_refresh_margin_seconds: int = field(
        default_factory=lambda: int(os.getenv("KIDS_RESOLVE_REFRESH_MARGIN_SECONDS", "300"))
    )
    kids_playback_min_remaining_seconds: int = field(
        default_factory=lambda: int(os.getenv("KIDS_PLAYBACK_MIN_REMAINING_SECONDS", "300"))
    )
    kids_ready_minimum: int = field(default_factory=lambda: int(os.getenv("KIDS_READY_MINIMUM", "18")))
    kids_channel_policy_version: str = field(
        default_factory=lambda: os.getenv("KIDS_CHANNEL_POLICY_VERSION", "sampled-channel-v1").strip()
    )
    kids_channel_recheck_seconds: int = field(
        default_factory=lambda: int(os.getenv("KIDS_CHANNEL_RECHECK_SECONDS", "604800"))
    )
    kids_channel_sample_size: int = field(
        default_factory=lambda: int(os.getenv("KIDS_CHANNEL_SAMPLE_SIZE", "8"))
    )
    kids_resolver_min_quality_height: int = field(default_factory=_kids_resolver_min_quality_height)


def get_host_timezone_name() -> str:
    tz = os.getenv("TZ")
    if tz:
        return tz
    try:
        return str(ZoneInfo("localtime"))
    except Exception:
        return "UTC"


DEFAULT_POLICY_FLAGS = {
    "block_cocomelon": True,
    "block_nursery_factory": True,
    "block_kids_clickbait_animals": True,
}

POLICY_PRESETS = [
    {
        "key": "block_cocomelon",
        "label": "Cocomelon",
        "description": "Always block Cocomelon songs/videos/channels.",
        "prompt_addon": 'ALWAYS BLOCK any content related to "cocomelon", including brand variants, channel names, thumbnails, and nursery-song compilations from this franchise.',
    },
    {
        "key": "block_nursery_factory",
        "label": "Nursery Factory / Clone Kids Songs",
        "description": "Block Cocomelon-like nursery-rhyme factory channels and clone content.",
        "prompt_addon": "ALWAYS BLOCK nursery-rhyme factory clone content, including repetitive toddler-song channels optimized for autoplay loops (for example: 'nursery rhymes', 'kids songs', 'for toddlers', and common clone channels).",
    },
    {
        "key": "block_kids_clickbait_animals",
        "label": "Kids Clickbait Animal Roleplay",
        "description": "Block exploitative monkey/animal clickbait roleplay content.",
        "prompt_addon": "ALWAYS BLOCK exploitative animal roleplay clickbait aimed at kids (for example monkey-baby toilet/pool prank loops, distress bait, or repetitive shock thumbnails).",
    },
    {
        "key": "block_skibidi",
        "label": "Skibidi / Skibidi Toilet",
        "description": "Brainrot-style chaotic meme animations.",
        "prompt_addon": 'BLOCK if content strongly matches keywords like "skibidi" or "skibidi toilet".',
    },
    {
        "key": "block_huggy_wuggy",
        "label": "Huggy Wuggy / Poppy Playtime",
        "description": "Toy-like horror monster content.",
        "prompt_addon": 'BLOCK if content matches "huggy wuggy", "poppy playtime", or close variants.',
    },
    {
        "key": "block_rainbow_friends",
        "label": "Rainbow Friends",
        "description": "Roblox-like horror with jumpscares.",
        "prompt_addon": 'BLOCK if content matches "rainbow friends" or similar horror gameplay for young kids.',
    },
    {
        "key": "block_siren_momo",
        "label": "Siren Head / Momo",
        "description": "Urban-legend horror characters.",
        "prompt_addon": 'BLOCK if content matches "siren head", "momo", or related horror urban legends.',
    },
    {
        "key": "block_prank",
        "label": "Prank",
        "description": "Bullying, rude, staged conflict behavior.",
        "prompt_addon": 'BLOCK prank-focused content, especially humiliation, bullying, or aggressive behavior.',
    },
    {
        "key": "block_challenge",
        "label": "Challenge",
        "description": "24-hour or dangerous challenge formats.",
        "prompt_addon": 'BLOCK risky challenge content, including "24 hour challenge" and physically dangerous stunts.',
    },
    {
        "key": "block_granny",
        "label": "Granny",
        "description": "Horror game around violent granny character.",
        "prompt_addon": 'BLOCK content matching the horror game "granny" and related clones.',
    },
    {
        "key": "block_fnaf",
        "label": "FNAF / Five Nights at Freddy's",
        "description": "Animatronic jumpscare horror.",
        "prompt_addon": 'BLOCK content matching "fnaf", "five nights at freddy", or animatronic jumpscare themes.',
    },
    {
        "key": "block_unboxing_eggs",
        "label": "Unboxing / Surprise Egg",
        "description": "Pure consumerist toy-promo loops.",
        "prompt_addon": 'BLOCK repetitive toy unboxing and surprise egg promotion content aimed at children.',
    },
    {
        "key": "block_kill_die",
        "label": "Kill / Killing / Die",
        "description": "Explicit violent title terms.",
        "prompt_addon": 'BLOCK when titles/context emphasize words like "kill", "killing", or "die".',
    },
    {
        "key": "block_blood_gore_horror",
        "label": "Blood / Gore / Horror",
        "description": "Visual violence and gore terms.",
        "prompt_addon": 'BLOCK if blood, gore, or explicit horror violence is central to the content.',
    },
    {
        "key": "block_guns_weapons",
        "label": "Guns / Shooting / Weapons",
        "description": "Firearms/weapon-centered content.",
        "prompt_addon": 'BLOCK if guns, shooting, or weapon-focused violence is a main theme.',
    },
    {
        "key": "block_elsagate_pregnant",
        "label": "Pregnant (Elsagate)",
        "description": "Fetish-like Elsagate mashups.",
        "prompt_addon": 'BLOCK Elsagate-like content involving "pregnant" cartoon or superhero mashups.',
    },
    {
        "key": "block_elsagate_injection",
        "label": "Injection / Doctor (Elsagate)",
        "description": "Needles/operations in disturbing kid animations.",
        "prompt_addon": 'BLOCK Elsagate-like content involving injections, needles, fake surgery, or forced doctor scenes.',
    },
    {
        "key": "block_suicide",
        "label": "Suicide / Self-harm",
        "description": "Self-harm and suicide themes.",
        "prompt_addon": 'BLOCK any self-harm or suicide-related content immediately.',
    },
]

SUPPORTED_TIMEZONES = [
    "UTC",
    "Europe/Amsterdam",
    "Europe/Brussels",
    "Europe/London",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "Asia/Tokyo",
    "Australia/Sydney",
]
