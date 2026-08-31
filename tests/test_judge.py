import pytest

from app.config import Settings
from app.db import Database
from app.services.blocklists import BlocklistService
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

    await db.set_setting("policy_flags_json", '{"block_prank": true}')
    out = await judge.evaluate(
        video_id="vid001",
        title="Staged prank compilation",
        channel_id="",
        channel_title="Kids Channel",
        video_url="https://www.youtube.com/watch?v=vid001",
    )
    assert out["verdict"] == "BLOCK"
    assert out["source"] == "policy"


@pytest.mark.asyncio
async def test_match_blocklist_uses_the_visible_local_blocklist(tmp_path):
    db_path = tmp_path / "sentinel.db"
    settings = Settings(db_path=str(db_path), data_dir=str(tmp_path / "data"))
    db = Database(str(db_path))
    await db.init()
    blocklists = BlocklistService(settings)
    await blocklists.save_local_content(
        "# configured by parent\nvideo:deny0000001 | blocked test item\n"
    )
    await blocklists.reload(db)
    judge = JudgeService(db, settings, WebhookClient(), blocklists=blocklists)

    decision = await judge.match_blocklist(
        video_id="deny0000001",
        title="Blocked test item",
        channel_id="",
        channel_title="Kids Channel",
        video_url="https://www.youtubekids.com/watch?v=deny0000001",
    )

    assert decision == {
        "verdict": "BLOCK",
        "reason": "Blocked by file blocklist (video)",
        "confidence": 100,
        "source": "file_blacklist",
    }


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
async def test_disabled_visible_policy_does_not_leave_hidden_keyword_block(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    settings = Settings()
    judge = JudgeService(db, settings, WebhookClient())
    await db.set_setting("policy_flags_json", '{"block_kids_clickbait_animals": false}')
    await db.set_setting("gemini_enabled", "false")

    out = await judge.evaluate(
        video_id="clickbait001",
        title="Monkey baby toilet story",
        channel_id="",
        channel_title="Family Channel",
        video_url="https://www.youtube.com/watch?v=clickbait001",
    )

    assert out["verdict"] == "ALLOW"
    assert out["source"] == "fallback"


@pytest.mark.asyncio
async def test_empty_policy_settings_do_not_enable_hidden_defaults(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    judge = JudgeService(db, Settings(), WebhookClient())
    await db.set_setting("policy_flags_json", "{}")
    await db.set_setting("gemini_enabled", "false")

    decision = await judge.match_blocklist(
        video_id="plain00001",
        title="Nursery rhymes compilation",
        channel_id="",
        channel_title="Family Channel",
        video_url="https://www.youtubekids.com/watch?v=plain00001",
    )

    assert decision is None


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
