import sqlite3
from datetime import datetime, timezone

import pytest

from app.db import Database
from tests.test_kids_resolver import eligible_item, approved_item_for_source


def mark_unassessed(db: Database, *item_ids: int) -> None:
    placeholders = ",".join("?" for _ in item_ids)
    with sqlite3.connect(db.db_path) as connection:
        connection.execute(
            f"""
            UPDATE catalog_items
            SET safety_verdict='UNCERTAIN',safety_checked_at=NULL,
                safety_policy_version='',safety_input_hash='',
                age_suitability_json='{{}}'
            WHERE id IN ({placeholders})
            """,
            item_ids,
        )
        connection.commit()


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


@pytest.mark.asyncio
async def test_assessment_limit_one_uses_deficit_after_budget_is_consumed(tmp_path):
    db = Database(str(tmp_path / "kids.db"))
    await db.init()
    fair_source = await eligible_item(db, "fair-source")
    deficit_source = await eligible_item(db, "deficit-source")
    fair_supply = [
        fair_source,
        await approved_item_for_source(
            db,
            fair_source["source_id"],
            fair_source["channel_id"],
            "fair-supply",
        ),
    ]
    for item in fair_supply:
        await db.catalog_item_safety_update(
            item["id"],
            verdict="SAFE",
            language="en",
            content_kind="entertainment",
            age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
            reason="established English entertainment supply",
            actor="test",
            correlation_id=f"fair-supply-{item['id']}",
        )
    deficit_supply = [
        deficit_source,
        await approved_item_for_source(
            db,
            deficit_source["source_id"],
            deficit_source["channel_id"],
            "deficit-supply-1",
        ),
        await approved_item_for_source(
            db,
            deficit_source["source_id"],
            deficit_source["channel_id"],
            "deficit-supply-2",
        ),
    ]
    for item in deficit_supply:
        await db.catalog_item_safety_update(
            item["id"],
            verdict="SAFE",
            language="en",
            content_kind="learning",
            age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
            reason="English item from Dutch source",
            actor="test",
            correlation_id=f"deficit-supply-{item['id']}",
        )
    fair_pending = await approved_item_for_source(
        db,
        fair_source["source_id"],
        fair_source["channel_id"],
        "fair-pending",
    )
    deficit_pending = await approved_item_for_source(
        db,
        deficit_source["source_id"],
        deficit_source["channel_id"],
        "deficit-pending",
    )
    await db.catalog_item_safety_update(
        fair_pending["id"],
        verdict="SAFE",
        language="en",
        content_kind="entertainment",
        age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
        reason="pending English entertainment",
        actor="test",
        correlation_id="fair-pending-safety",
    )
    mark_unassessed(db, fair_pending["id"], deficit_pending["id"])
    day = datetime.now(timezone.utc).date().isoformat()
    await db.set_setting("kids_item_assessment_budget_day", day)
    await db.set_setting("kids_item_assessment_budget_count", "0")

    fair = await db.kids_item_assessment_candidates(limit=1)
    assert fair[0]["item"]["source_id"] == fair_source["source_id"]
    assert await db.kids_item_assessment_budget_take(400)

    deficit = await db.kids_item_assessment_candidates(limit=1)
    assert deficit[0]["item"]["source_id"] == deficit_source["source_id"]


@pytest.mark.asyncio
async def test_current_uncertain_is_skipped_until_item_policy_changes(tmp_path):
    db = Database(str(tmp_path / "kids.db"))
    await db.init()
    item = await eligible_item(db, "current-uncertain")
    await db.catalog_item_safety_update(
        item["id"],
        verdict="UNCERTAIN",
        language="nl",
        content_kind="learning",
        age_suitability={"2": "SUITABLE", "6": "SUITABLE"},
        reason="current uncertainty",
        actor="test",
        correlation_id="current-uncertain",
    )

    assert await db.kids_item_assessment_candidates(limit=1) == []

    db.kids_item_policy_version = "kids-item-safety-v2"
    candidates = await db.kids_item_assessment_candidates(limit=1)
    assert candidates[0]["item"]["id"] == item["id"]


@pytest.mark.asyncio
async def test_assessment_deficit_uses_remaining_source_hints_after_fair_slot(tmp_path):
    db = Database(str(tmp_path / "kids.db"))
    await db.init()
    source_a = await eligible_item(db, "stale-hints-a")
    source_b = await eligible_item(db, "stale-hints-b")
    for index in range(2):
        await approved_item_for_source(
            db,
            source_b["source_id"],
            source_b["channel_id"],
            f"stale-hints-b-assessed-{index}",
        )
    a_fair = await approved_item_for_source(
        db,
        source_a["source_id"],
        source_a["channel_id"],
        "stale-hints-a-fair",
    )
    a_deficit = await approved_item_for_source(
        db,
        source_a["source_id"],
        source_a["channel_id"],
        "stale-hints-a-deficit",
    )
    b_deficit = await approved_item_for_source(
        db,
        source_b["source_id"],
        source_b["channel_id"],
        "stale-hints-b-deficit",
    )
    await db.catalog_item_safety_update(
        a_fair["id"],
        verdict="SAFE",
        language="nl",
        content_kind="entertainment",
        age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
        reason="fair fun-nl hint",
        actor="test",
        correlation_id="stale-hints-a-fair",
    )
    await db.catalog_item_safety_update(
        a_deficit["id"],
        verdict="SAFE",
        language="en",
        content_kind="learning",
        age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
        reason="English item from Dutch source",
        actor="test",
        correlation_id="stale-hints-a-deficit",
    )
    await db.catalog_item_safety_update(
        b_deficit["id"],
        verdict="SAFE",
        language="en",
        content_kind="entertainment",
        age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
        reason="remaining fun-en hint",
        actor="test",
        correlation_id="stale-hints-b-deficit",
    )
    mark_unassessed(db, a_fair["id"], a_deficit["id"], b_deficit["id"])
    candidates = await db.kids_item_assessment_candidates(limit=2)

    assert [entry["item"]["id"] for entry in candidates] == [
        a_fair["id"],
        b_deficit["id"],
    ]


@pytest.mark.asyncio
async def test_assessment_deficit_excludes_recent_and_owned_shelves(tmp_path):
    db = Database(str(tmp_path / "kids.db"))
    await db.init()
    target_source = await eligible_item(db, "cooldown-owned-target")
    other_source = await eligible_item(db, "cooldown-owned-other")
    await db.catalog_item_safety_update(
        target_source["id"],
        verdict="SAFE",
        language="nl",
        content_kind="entertainment",
        age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
        reason="established Dutch entertainment supply",
        actor="test",
        correlation_id="cooldown-owned-target-supply",
    )
    target_fun_supply = await approved_item_for_source(
        db,
        target_source["source_id"],
        target_source["channel_id"],
        "cooldown-owned-fun-supply",
    )
    await db.catalog_item_safety_update(
        target_fun_supply["id"],
        verdict="SAFE",
        language="nl",
        content_kind="entertainment",
        age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
        reason="established Dutch entertainment supply",
        actor="test",
        correlation_id="cooldown-owned-fun-supply",
    )
    await db.catalog_item_safety_update(
        other_source["id"],
        verdict="SAFE",
        language="en",
        content_kind="learning",
        age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
        reason="English item from Dutch source",
        actor="test",
        correlation_id="cooldown-owned-other-supply",
    )
    recent = await approved_item_for_source(
        db,
        target_source["source_id"],
        target_source["channel_id"],
        "cooldown-owned-recent",
    )
    owned = await approved_item_for_source(
        db,
        target_source["source_id"],
        target_source["channel_id"],
        "cooldown-owned-fun-nl",
    )
    await db.catalog_item_safety_update(
        owned["id"],
        verdict="SAFE",
        language="mixed",
        content_kind="mixed",
        age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
        reason="mixed item already owned by fun-nl",
        actor="test",
        correlation_id="cooldown-owned-fun-nl",
    )
    unowned = await approved_item_for_source(
        db,
        target_source["source_id"],
        target_source["channel_id"],
        "cooldown-owned-learning",
    )
    other_pending = await approved_item_for_source(
        db,
        other_source["source_id"],
        other_source["channel_id"],
        "cooldown-owned-other-pending",
    )
    await db.catalog_item_safety_update(
        other_pending["id"],
        verdict="SAFE",
        language="en",
        content_kind="learning",
        age_suitability={"2": "UNSUITABLE", "6": "SUITABLE"},
        reason="English item from Dutch source",
        actor="test",
        correlation_id="cooldown-owned-other-pending",
    )
    await db.kids_watch_event_record(
        video_id=recent["video_id"],
        event="completed",
        profile="noah",
        position_seconds=60,
        session_id="cooldown-owned-session",
        startup_ms=None,
        correlation_id="cooldown-owned-completed",
    )
    day = datetime.now(timezone.utc).date().isoformat()
    await db.kids_daily_library_get_or_create(
        day=day,
        profile="noah",
        shelf_limit=1,
        proposed_item_ids={"fun-nl": [owned["id"]]},
    )
    mark_unassessed(
        db,
        recent["id"],
        owned["id"],
        unowned["id"],
        other_pending["id"],
    )
    await db.set_setting("kids_item_assessment_budget_day", day)
    await db.set_setting("kids_item_assessment_budget_count", "0")

    fairness = await db.kids_item_assessment_candidates(limit=1)
    assert fairness[0]["item"]["id"] == other_pending["id"]
    assert await db.kids_item_assessment_budget_take(400)

    deficit = await db.kids_item_assessment_candidates(limit=1)
    assert deficit[0]["item"]["id"] == unowned["id"]


@pytest.mark.asyncio
async def test_daily_new_item_does_not_cover_a_content_shelf_deficit(tmp_path):
    db = Database(str(tmp_path / "kids.db"))
    await db.init()
    other_source = await eligible_item(db, "other-source")
    daily_source = await eligible_item(db, "daily-new-source")
    await db.catalog_item_safety_update(
        other_source["id"],
        verdict="SAFE",
        language="en",
        content_kind="learning",
        age_suitability={"2": "SUITABLE", "6": "SUITABLE"},
        reason="English item from Dutch source",
        actor="test",
        correlation_id="other-item-safety",
    )
    daily_pending = await approved_item_for_source(
        db,
        daily_source["source_id"],
        daily_source["channel_id"],
        "daily-new-pending",
    )
    other_pending = await approved_item_for_source(
        db,
        other_source["source_id"],
        other_source["channel_id"],
        "other-pending",
    )
    mark_unassessed(db, daily_pending["id"], other_pending["id"])
    await db.kids_daily_library_get_or_create(
        day=datetime.now(timezone.utc).date().isoformat(),
        profile="noah",
        shelf_limit=1,
        proposed_item_ids={"new": [daily_source["id"]]},
    )
    await db.set_setting(
        "kids_item_assessment_budget_day",
        datetime.now(timezone.utc).date().isoformat(),
    )
    await db.set_setting("kids_item_assessment_budget_count", "1")

    candidates = await db.kids_item_assessment_candidates(limit=1)

    assert candidates[0]["item"]["source_id"] == daily_source["source_id"]


@pytest.mark.asyncio
async def test_editorial_reserve_uses_budget_cadence_for_batch_of_six(tmp_path):
    db = Database(str(tmp_path / "kids.db"))
    await db.init()
    safety_source = await eligible_item(db, "cadence-safety")
    editorial_source = await eligible_item(db, "cadence-editorial")
    safety_items = [
        await approved_item_for_source(
            db, safety_source["source_id"], safety_source["channel_id"], f"cadence-safety-{i}"
        )
        for i in range(6)
    ]
    editorial_items = [
        await approved_item_for_source(
            db, editorial_source["source_id"], editorial_source["channel_id"], f"cadence-editorial-{i}"
        )
        for i in range(3)
    ]
    mark_unassessed(db, *(item["id"] for item in safety_items))
    for item in editorial_items[:2]:
        await db.kids_item_editorial(
            item["id"],
            {"target_audience": "school_age", "category": "other", "reason": "done"},
        )
    await db.set_setting("kids_item_assessment_budget_day", datetime.now(timezone.utc).date().isoformat())
    await db.set_setting("kids_item_assessment_budget_count", "9")

    candidates = await db.kids_item_assessment_candidates(limit=6)

    assert len(candidates) == 6
    assert sum(entry.get("editorial_only", False) for entry in candidates) == 1
    assert candidates[0]["editorial_only"] is True


@pytest.mark.asyncio
async def test_editorial_cadence_advances_after_tenth_budget_slot(tmp_path):
    db = Database(str(tmp_path / "kids.db"))
    await db.init()
    editorial = await eligible_item(db, "cadence-advance")
    await db.kids_item_editorial(
        editorial["id"],
        {"target_audience": "school_age", "category": "other", "reason": "done"},
    )
    second = await approved_item_for_source(
        db, editorial["source_id"], editorial["channel_id"], "cadence-advance-2"
    )
    mark_unassessed(db, second["id"])
    await db.set_setting("kids_item_assessment_budget_day", datetime.now(timezone.utc).date().isoformat())
    await db.set_setting("kids_item_assessment_budget_count", "10")

    candidates = await db.kids_item_assessment_candidates(limit=6)

    assert [entry["item"]["id"] for entry in candidates] == [second["id"]]
    assert all(not entry.get("editorial_only") for entry in candidates)


@pytest.mark.asyncio
async def test_parent_editorial_stale_hash_is_never_selected(tmp_path):
    db = Database(str(tmp_path / "kids.db"))
    await db.init()
    item = await eligible_item(db, "parent-editorial")
    await db.kids_item_editorial(
        item["id"],
        {"target_audience": "school_age", "category": "other", "reason": "parent"},
    )
    with sqlite3.connect(db.db_path) as connection:
        connection.execute("UPDATE catalog_items SET title='changed' WHERE id=?", (item["id"],))
        connection.commit()
    refreshed = await db.catalog_get("item", item["id"])
    await db.catalog_item_safety_update(
        item["id"],
        verdict=refreshed["safety_verdict"],
        language=refreshed["language"],
        content_kind=refreshed["content_kind"],
        age_suitability={"2": "SUITABLE", "6": "SUITABLE"},
        reason=refreshed["safety_reason"],
        actor="test",
        correlation_id="parent-editorial-refresh",
    )

    candidates = await db.kids_item_assessment_candidates(limit=6)
    assert all(not entry.get("editorial_only") for entry in candidates)


@pytest.mark.asyncio
async def test_editorial_interleave_respects_near_exhausted_budget(tmp_path):
    db = Database(str(tmp_path / "kids.db"))
    await db.init()
    safety_source = await eligible_item(db, "near-limit-safety")
    editorial_source = await eligible_item(db, "near-limit-editorial")
    safety_items = [safety_source] + [
        await approved_item_for_source(
            db, safety_source["source_id"], safety_source["channel_id"], f"near-limit-safety-{i}"
        )
        for i in range(1)
    ]
    editorial = await approved_item_for_source(
        db, editorial_source["source_id"], editorial_source["channel_id"], "near-limit-editorial-item"
    )
    await db.kids_item_editorial(
        editorial_source["id"],
        {"target_audience": "school_age", "category": "other", "reason": "done"},
    )
    mark_unassessed(db, *(item["id"] for item in safety_items))
    day = datetime.now(timezone.utc).date().isoformat()
    await db.set_setting("kids_item_assessment_budget_day", day)
    await db.set_setting("kids_item_assessment_budget_count", "198")

    candidates = await db.kids_item_assessment_candidates(
        limit=6, daily_limit=200
    )

    assert len(candidates) == 2
    assert candidates[0]["item"]["id"] == safety_items[0]["id"]
    assert candidates[1]["item"]["id"] == editorial["id"]
    assert candidates[1]["editorial_only"] is True


@pytest.mark.asyncio
async def test_editorial_fills_spare_batch_slots_after_last_safety_item(tmp_path):
    db = Database(str(tmp_path / "kids.db"))
    await db.init()
    safety_source = await eligible_item(db, "spare-safety")
    editorial_source = await eligible_item(db, "spare-editorial")
    safety = await approved_item_for_source(
        db, safety_source["source_id"], safety_source["channel_id"], "spare-safety-item"
    )
    editorials = [
        await approved_item_for_source(
            db, editorial_source["source_id"], editorial_source["channel_id"], f"spare-editorial-{i}"
        )
        for i in range(3)
    ]
    await db.kids_item_editorial(
        editorial_source["id"],
        {"target_audience": "school_age", "category": "other", "reason": "done"},
    )
    await db.kids_item_editorial(
        safety_source["id"],
        {"target_audience": "school_age", "category": "other", "reason": "done"},
    )
    await db.kids_item_editorial(
        safety["id"],
        {"target_audience": "school_age", "category": "other", "reason": "pending"},
    )
    mark_unassessed(db, safety["id"])

    candidates = await db.kids_item_assessment_candidates(
        limit=6, daily_limit=200
    )

    assert [entry["item"]["id"] for entry in candidates] == [
        safety["id"], *(item["id"] for item in editorials)
    ]
    assert all(entry.get("editorial_only") for entry in candidates[1:])
