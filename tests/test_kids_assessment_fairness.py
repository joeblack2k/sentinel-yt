import sqlite3

import pytest

from app.db import Database
from tests.test_kids_resolver import eligible_item, approved_item_for_source


@pytest.mark.asyncio
async def test_assessment_reaches_underrepresented_sources_across_batches(tmp_path):
    db = Database(str(tmp_path / "kids.db"))
    await db.init()
    sources = [await eligible_item(db, name) for name in ("established", "lego", "science")]
    pending = []
    for source in sources:
        for index in range(3):
            item = await approved_item_for_source(db, source["source_id"], source["channel_id"],
                                                 f"{source['video_id']}-{index}")
            pending.append(item["id"])
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("UPDATE catalog_items SET safety_checked_at=NULL WHERE id IN (%s)" %
                     ",".join("?" for _ in pending), pending)
        conn.execute("UPDATE catalog_items SET safety_checked_at=NULL WHERE id IN (?,?)",
                     (sources[1]["id"], sources[2]["id"]))
    first = (await db.kids_item_assessment_candidates(limit=1))[0]["item"]
    assert first["source_id"] == sources[1]["source_id"]
    await db.catalog_item_safety_update(first["id"], verdict="SAFE", language="nl",
        content_kind="learning", age_suitability={"2": "SUITABLE", "6": "SUITABLE"},
        reason="test", actor="test", correlation_id="first-assessment")
    second = (await db.kids_item_assessment_candidates(limit=1))[0]["item"]
    assert second["source_id"] == sources[2]["source_id"]
    batch = await db.kids_item_assessment_candidates(limit=3)
    assert len({entry["item"]["source_id"] for entry in batch}) == 3
