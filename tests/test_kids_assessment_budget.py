import asyncio
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.db import Database


@pytest.mark.asyncio
async def test_raised_budget_preserves_usage_and_remains_atomic(tmp_path, monkeypatch):
    db = Database(str(tmp_path / 'kids.db'))
    await db.init()
    await db.set_setting('kids_item_assessment_budget_day', datetime.now(timezone.utc).date().isoformat())
    await db.set_setting('kids_item_assessment_budget_count', '200')
    assert not await db.kids_item_assessment_budget_take(200)
    monkeypatch.setenv('KIDS_ITEM_ASSESSMENT_DAILY_LIMIT', '400')
    assert Settings().kids_item_assessment_daily_limit == 400
    assert await db.kids_item_assessment_budget_take(400)
    assert await db.get_setting('kids_item_assessment_budget_count') == '201'
    await db.set_setting('kids_item_assessment_budget_count', '399')
    results = await asyncio.gather(*(db.kids_item_assessment_budget_take(9999) for _ in range(3)))
    assert sum(results) == 1
    assert await db.get_setting('kids_item_assessment_budget_count') == '400'
    monkeypatch.setenv('KIDS_ITEM_ASSESSMENT_DAILY_LIMIT', '9999')
    assert Settings().kids_item_assessment_daily_limit == 400
    assert not await db.kids_item_assessment_budget_take(0)
