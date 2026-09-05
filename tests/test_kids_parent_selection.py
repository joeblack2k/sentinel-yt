import asyncio
import json

import aiosqlite
import pytest

from app.db import Database
from app.services.kids_ingest import apply_channel_classification


@pytest.mark.parametrize("selection", [[], ["felix"], ["noah"]])
def test_parent_selection_survives_rejudge_and_restart(tmp_path, selection):
    async def check():
        db = Database(str(tmp_path / "kids.db"))
        await db.init()
        source = await db.catalog_create("source", {
            "kind": "channel", "reference": "UC-parent-selection",
            "title": "Profile selection", "profile_slugs": [], "correlation_id": "seed",
        })
        decision = {"verdict": "SAFE", "language": "nl", "content_kind": "learning",
                    "age_suitability": {"2": "SUITABLE", "6": "SUITABLE"}, "reason": "test"}

        async def judge(value):
            return await apply_channel_classification(
                db, source, value, evidence=[], policy_version="test",
                actor="judge", correlation_id="rejudge",
            )

        await judge(decision)
        assert await db.kids_source_profile_slugs(source["id"]) == ["felix", "noah"]
        await db.kids_source_profiles_set(source["id"], selection, actor="parent-ui",
            reason="interest selection", correlation_id="parent", persist_parent_selection=True)
        await db.init()
        await judge(decision)
        assert await db.kids_source_profile_slugs(source["id"]) == selection
        await judge({**decision, "verdict": "UNSAFE"})
        assert await db.kids_source_profile_slugs(source["id"]) == []
        # Reapproval cannot introduce a profile outside the stored parent choice.
        row = await db.catalog_get("source", source["id"])
        assert row["parent_profile_slugs_json"] is not None
        await judge(decision)
        assert set(await db.kids_source_profile_slugs(source["id"])) <= set(selection)
        with pytest.raises(ValueError):
            await db.kids_source_profiles_set(source["id"], ["other"], actor="parent-ui",
                reason="invalid", correlation_id="invalid", persist_parent_selection=True)
        assert (await db.catalog_get("source", source["id"]))["parent_profile_slugs_json"] == row["parent_profile_slugs_json"]
        await db.kids_source_profiles_set(source["id"], ["felix", "noah"], actor="parent-ui",
            reason="blocked parent choice", correlation_id="blocked-parent", persist_parent_selection=True)
        assert await db.kids_source_profile_slugs(source["id"]) == []
        async with aiosqlite.connect(db.db_path) as conn:
            audit = await (await conn.execute(
                "SELECT reason FROM kids_audit_events WHERE correlation_id='blocked-parent'"
            )).fetchone()
            assert 'parent_profiles=["felix", "noah"]' in audit[0]
            assert audit[0].endswith("profiles=none")

    asyncio.run(check())


def test_explicit_empty_selection_is_audited_and_clears_stale_slots(tmp_path):
    async def check():
        db = Database(str(tmp_path / "kids.db"))
        await db.init()
        source = await db.catalog_create("source", {
            "kind": "channel", "reference": "UC-empty-parent",
            "profile_slugs": [], "correlation_id": "seed",
        })
        item = await db.catalog_create("item", {
            "video_id": "empty-parent", "source_id": source["id"], "correlation_id": "item",
        })
        async with aiosqlite.connect(db.db_path) as conn:
            await conn.execute(
                "INSERT INTO kids_daily_library(day,profile,shelf,ordinal,item_id,created_at) VALUES(?,?,?,?,?,?)",
                ("2026-09-05", "noah", "new", 0, item["id"], "2026-09-05"),
            )
            await conn.commit()
        await db.kids_source_profiles_set(source["id"], [], actor="parent-ui",
            reason="neither", correlation_id="explicit-empty", persist_parent_selection=True)
        row = await db.catalog_get("source", source["id"])
        assert json.loads(row["parent_profile_slugs_json"]) == []
        async with aiosqlite.connect(db.db_path) as conn:
            assert (await (await conn.execute("SELECT item_id FROM kids_daily_library")).fetchone())[0] is None
            event = await (await conn.execute(
                "SELECT event FROM kids_audit_events WHERE correlation_id='explicit-empty'"
            )).fetchone()
            assert event[0] == "source_parent_profiles_changed"
        await apply_channel_classification(db, source, {
            "verdict": "SAFE", "language": "nl", "content_kind": "learning",
            "age_suitability": {"2": "SUITABLE", "6": "SUITABLE"}, "reason": "test",
        }, evidence=[], policy_version="test", actor="judge", correlation_id="rejudge")
        assert await db.kids_source_profile_slugs(source["id"]) == []

    asyncio.run(check())
