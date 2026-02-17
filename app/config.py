from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    app_name: str = "Sentinel"
    build_version: str = field(default_factory=lambda: os.getenv("SENTINEL_BUILD_VERSION", "v1"))
    host: str = field(default_factory=lambda: os.getenv("SENTINEL_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("SENTINEL_PORT", "8090")))
    data_dir: str = field(default_factory=lambda: os.getenv("SENTINEL_DATA_DIR", "/data"))
    db_path: str = field(default_factory=lambda: os.getenv("SENTINEL_DB_PATH", "/data/sentinel.db"))
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.0-flash"))
    schedule_timezone_default: str = field(default_factory=lambda: os.getenv("SENTINEL_TIMEZONE_DEFAULT", ""))
    webhook_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("SENTINEL_WEBHOOK_TIMEOUT_SECONDS", "8"))
    )
    decision_cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("SENTINEL_DECISION_CACHE_TTL_SECONDS", "2592000"))
    )
    strict_allow_min_confidence: int = field(
        default_factory=lambda: int(os.getenv("SENTINEL_STRICT_ALLOW_MIN_CONFIDENCE", "95"))
    )
    sponsorblock_api_base: str = field(
        default_factory=lambda: os.getenv("SENTINEL_SPONSORBLOCK_API_BASE", "https://sponsor.ajay.app/api")
    )
    sponsorblock_segment_cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("SENTINEL_SPONSORBLOCK_SEGMENT_CACHE_TTL_SECONDS", "900"))
    )
    remote_blocklists_cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("SENTINEL_REMOTE_BLOCKLISTS_CACHE_TTL_SECONDS", "900"))
    )


def get_host_timezone_name() -> str:
    tz = os.getenv("TZ")
    if tz:
        return tz
    try:
        return str(ZoneInfo("localtime"))
    except Exception:
        return "UTC"


DEFAULT_SAFE_PROMPT = (
    "You are Sentinel, a very strict child safety and anti-brainrot YouTube guardian for a 6-year-old child. "
    "Classify videos conservatively and prefer BLOCK on uncertainty. Always block highly stimulating, addictive, low-value spam, "
    "shouting, manipulative engagement loops, and age-inappropriate themes. "
    "Treat 'nursery-rhyme factory' videos (algorithmic toddler-song loops with bright overstimulating visuals, repetitive hooks, "
    "or copycat channels) as unsafe by default unless there is clear educational value and calm pacing. "
    "Treat exploitative animal roleplay/clickbait videos (for example monkey-baby prank/toilet/pool roleplay loops) as unsafe for children. "
    "Consider child safety, language, visuals, and educational value."
)

DEFAULT_WHITELIST_PROMPT = (
    "You are Sentinel in WHITELIST mode for a 6-year-old child. "
    "Only allow content that clearly matches the active allow-profile categories. "
    "If the video does not clearly fit those categories, return BLOCK. "
    "Prefer BLOCK on uncertainty."
)

OUTPUT_CONTRACT_SUFFIX = (
    "\n\nReturn ONLY valid JSON with this exact schema and keys: "
    '{"verdict":"ALLOW"|"BLOCK","reason":"string","confidence":0-100}. '
    "No markdown, no extra keys, no extra text."
)

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

ALLOW_POLICY_PRESETS = [
    {
        "key": "allow_90s_cartoons",
        "label": "90s Cartoons",
        "description": "Classic 1990s cartoons from major kids networks.",
        "prompt_addon": "ALLOW classic 1990s cartoons and franchise content aimed at children.",
    },
    {
        "key": "allow_00s_cartoons",
        "label": "00s Cartoons",
        "description": "Classic 2000s cartoons from major kids networks.",
        "prompt_addon": "ALLOW classic 2000s cartoons and age-appropriate animated series.",
    },
    {
        "key": "allow_all_cartoons",
        "label": "All Cartoons",
        "description": "Allow family-safe animation from trusted channels.",
        "prompt_addon": "ALLOW family-safe cartoons and animated shorts from trusted channels.",
    },
    {
        "key": "allow_disney_family",
        "label": "Disney",
        "description": "Disney and Disney Junior family-safe content.",
        "prompt_addon": "ALLOW family-safe Disney, Disney Junior, and Pixar-style kids content.",
    },
    {
        "key": "allow_educational",
        "label": "Educational",
        "description": "School-friendly educational content for kids.",
        "prompt_addon": "ALLOW educational content for children: literacy, math, science, geography, and life skills.",
    },
    {
        "key": "allow_religion",
        "label": "Religion",
        "description": "Age-appropriate faith and values content.",
        "prompt_addon": "ALLOW calm, age-appropriate faith and values content without fear-based messaging.",
    },
    {
        "key": "allow_pbs_kids",
        "label": "PBS Kids Classics",
        "description": "Trusted PBS-style educational shows.",
        "prompt_addon": "ALLOW PBS Kids style educational programming and classic learning shows.",
    },
    {
        "key": "allow_nickelodeon_90s",
        "label": "Nickelodeon Classics",
        "description": "Nickelodeon classics popular in the 1990s/2000s.",
        "prompt_addon": "ALLOW family-safe Nickelodeon classics suitable for young children.",
    },
    {
        "key": "allow_cartoon_network_classics",
        "label": "Cartoon Network Classics",
        "description": "Classic Cartoon Network shows and clips.",
        "prompt_addon": "ALLOW classic Cartoon Network family-safe cartoon content.",
    },
    {
        "key": "allow_disney_afternoon",
        "label": "Disney Afternoon Classics",
        "description": "DuckTales/TaleSpin-like classic Disney afternoon content.",
        "prompt_addon": "ALLOW Disney Afternoon style family-safe classics.",
    },
    {
        "key": "allow_animal_documentaries",
        "label": "Animal Documentaries",
        "description": "Calm, educational animal documentaries.",
        "prompt_addon": "ALLOW educational animal documentaries with calm narration and no distress bait.",
    },
    {
        "key": "allow_nature_science",
        "label": "Nature & Science",
        "description": "Nature, space, and science explainers for kids.",
        "prompt_addon": "ALLOW child-friendly nature, space, and science explainers.",
    },
    {
        "key": "allow_music_rhythm",
        "label": "Music & Rhythm",
        "description": "Age-appropriate music and rhythm learning.",
        "prompt_addon": "ALLOW age-appropriate music, rhythm, and movement learning content.",
    },
    {
        "key": "allow_arts_crafts",
        "label": "Arts & Crafts",
        "description": "Drawing, craft, and making videos for children.",
        "prompt_addon": "ALLOW arts and crafts tutorials suitable for children.",
    },
    {
        "key": "allow_storytelling_books",
        "label": "Storytelling & Books",
        "description": "Read-aloud and storytelling videos.",
        "prompt_addon": "ALLOW calm storytelling, read-aloud, and children's books content.",
    },
    {
        "key": "allow_family_game_shows",
        "label": "Family Game Shows",
        "description": "Family-friendly quiz and game formats.",
        "prompt_addon": "ALLOW child-friendly quiz and family game content without humiliation or risky challenges.",
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

DEFAULT_SPONSORBLOCK_CATEGORIES = [
    "sponsor",
    "selfpromo",
    "interaction",
    "intro",
    "outro",
    "music_offtopic",
]
