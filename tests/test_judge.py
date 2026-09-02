import pytest

from app.config import Settings
from app.db import Database
from app.services.blocklists import BlocklistService
from app.services.judge import JudgeService


@pytest.mark.asyncio
async def test_policy_toggle_local_keyword_block(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    judge = JudgeService(db)

    await db.set_setting("policy_flags_json", '{"block_prank": true}')
    out = await judge.match_blocklist(
        video_id="vid001",
        title="Staged prank compilation",
        channel_id="",
        channel_title="Kids Channel",
        video_url="https://www.youtubekids.com/watch?v=vid001",
    )
    assert out == {
        "verdict": "BLOCK",
        "reason": "guardian_policy_block_prank",
        "confidence": 100,
        "source": "policy",
    }


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
    judge = JudgeService(db, blocklists=blocklists)

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
async def test_empty_policy_settings_do_not_enable_hidden_defaults(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    judge = JudgeService(db)
    await db.set_setting("policy_flags_json", "{}")

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
    judge = JudgeService(db)

    await db.add_rule("blacklist", "video", "abc123")
    out = await judge.match_blocklist(
        video_id="abc123",
        title="",
        channel_id="",
        channel_title="",
        video_url="https://www.youtubekids.com/watch?v=abc123",
    )
    assert out == {
        "verdict": "BLOCK",
        "reason": "Blocked by local blacklist (video)",
        "confidence": 100,
        "source": "blacklist",
    }
