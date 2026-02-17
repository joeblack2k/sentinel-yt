import pytest

from app.config import Settings
from app.db import Database
from app.services.judge import JudgeService
from app.services.webhook import WebhookClient


@pytest.mark.asyncio
async def test_judge_prompt_contract_and_parse(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    settings = Settings()
    judge = JudgeService(db, settings, WebhookClient())

    await db.set_setting("custom_prompt", "Be strict")
    prompt = await judge._effective_prompt()
    assert "Be strict" in prompt
    assert "Return ONLY valid JSON" in prompt

    parsed = judge._parse_output('{"verdict":"ALLOW","reason":"ok","confidence":88}')
    assert parsed["verdict"] == "ALLOW"
    assert parsed["confidence"] == 88


@pytest.mark.asyncio
async def test_policy_prompt_addon_enabled(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    settings = Settings()
    judge = JudgeService(db, settings, WebhookClient())

    await db.set_setting("policy_flags_json", '{"block_skibidi": true, "block_suicide": true}')
    prompt = await judge._effective_prompt()
    assert "Skibidi / Skibidi Toilet" in prompt
    assert "Suicide / Self-harm" in prompt


@pytest.mark.asyncio
async def test_policy_toggle_local_keyword_block(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    settings = Settings()
    judge = JudgeService(db, settings, WebhookClient())

    await db.set_setting("policy_flags_json", '{"block_cocomelon": true}')
    out = await judge.evaluate(
        video_id="vid001",
        title="COCOMELON nursery rhymes compilation",
        channel_id="",
        channel_title="Kids Channel",
        video_url="https://www.youtube.com/watch?v=vid001",
    )
    assert out["verdict"] == "BLOCK"
    assert out["source"] == "policy"


@pytest.mark.asyncio
async def test_default_nursery_factory_policy_blocks_without_manual_flags(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    settings = Settings()
    judge = JudgeService(db, settings, WebhookClient())

    out = await judge.evaluate(
        video_id="vid-nursery-001",
        title="Dinosaur Monster Song | Baby Anna Kids Songs",
        channel_id="",
        channel_title="Baby Anna - Kids Songs",
        video_url="https://www.youtube.com/watch?v=vid-nursery-001",
    )
    assert out["verdict"] == "BLOCK"
    assert out["source"] == "policy"


@pytest.mark.asyncio
async def test_strict_allow_gate_blocks_low_confidence_allow(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    settings = Settings()
    judge = JudgeService(db, settings, WebhookClient())

    await db.cache_set(
        "blocklist:lowconf001",
        {"verdict": "ALLOW", "reason": "model unsure", "confidence": 70, "source": "gemini"},
        "2099-01-01T00:00:00+00:00",
    )
    out = await judge.evaluate(
        video_id="lowconf001",
        title="Calm educational clip",
        channel_id="",
        channel_title="Trusted Education",
        video_url="https://www.youtube.com/watch?v=lowconf001",
    )
    assert out["verdict"] == "BLOCK"
    assert out["source"] == "policy"


@pytest.mark.asyncio
async def test_rule_precedence_blacklist(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    settings = Settings()
    judge = JudgeService(db, settings, WebhookClient())

    await db.add_rule("blacklist", "video", "abc123")
    out = await judge.evaluate(
        video_id="abc123",
        title="",
        channel_id="",
        channel_title="",
        video_url="https://www.youtube.com/watch?v=abc123",
    )
    assert out["verdict"] == "BLOCK"
    assert out["source"] == "blacklist"


@pytest.mark.asyncio
async def test_whitelist_mode_blocks_when_no_allow_match_and_gemini_disabled(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    settings = Settings()
    judge = JudgeService(db, settings, WebhookClient())
    await db.set_setting("gemini_enabled", "false")

    out = await judge.evaluate(
        video_id="noallow001",
        title="Random video",
        channel_id="",
        channel_title="Random channel",
        video_url="https://www.youtube.com/watch?v=noallow001",
        enforcement_mode="whitelist",
    )
    assert out["verdict"] == "BLOCK"


@pytest.mark.asyncio
async def test_whitelist_mode_allows_local_whitelist(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    settings = Settings()
    judge = JudgeService(db, settings, WebhookClient())
    await db.add_rule("whitelist", "video", "allow001")

    out = await judge.evaluate(
        video_id="allow001",
        title="Some title",
        channel_id="",
        channel_title="Some channel",
        video_url="https://www.youtube.com/watch?v=allow001",
        enforcement_mode="whitelist",
    )
    assert out["verdict"] == "ALLOW"
    assert out["source"] == "whitelist"
