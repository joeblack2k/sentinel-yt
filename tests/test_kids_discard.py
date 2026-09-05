import sqlite3

import pytest

from app.db import Database
from app.services.kids_ingest import apply_channel_classification, ingest_once, HOME_SOURCE_REFERENCE
from tests.test_kids_ingest import FakeClassifier
from tests.test_kids_resolver import eligible_item


@pytest.mark.asyncio
async def test_discard_is_permanent_visible_and_keeps_safety_verdict(tmp_path):
    db = Database(str(tmp_path / 'kids.db'))
    await db.init()
    item = await eligible_item(db, 'discard')
    source_id = item['source_id']
    reason = 'parent_discard:interest: Outside family interests'
    await db.catalog_transition('source', source_id, {
        'state': 'revoked', 'actor': 'parent-ui', 'reason': reason, 'correlation_id': 'discard',
    })
    for state in ('approved', 'candidate', 'unknown', 'blocked'):
        with pytest.raises(ValueError):
            await db.catalog_transition('source', source_id, {
                'state': state, 'actor': 'judge', 'reason': 'retry', 'correlation_id': 'retry',
            })
    source = await db.catalog_get('source', source_id)
    await apply_channel_classification(db, source, {
        'verdict': 'SAFE', 'language': 'en', 'content_kind': 'learning',
        'age_suitability': {'2': 'SUITABLE', '6': 'SUITABLE'}, 'reason': 'new analysis',
    }, evidence=[], policy_version='test', actor='judge', correlation_id='rejudge')
    row = (await db.catalog_sources_list(state='revoked'))[0]
    assert row['discard_reason'] == reason
    assert row['safety_verdict'] == 'SAFE'
    assert row['profile_slugs'] == []
    assert row['parent_profile_slugs_json'] == '[]'
    assert await db.kids_item_assessment_candidates(limit=10) == []
    assert await db.kids_eligible_feed_list(300, profile='noah') == []


@pytest.mark.asyncio
async def test_discarded_channel_beyond_list_limit_is_not_rediscovered(tmp_path):
    db = Database(str(tmp_path / 'kids.db'))
    await db.init()
    with sqlite3.connect(db.db_path) as conn:
        conn.executemany(
            "INSERT INTO catalog_sources(kind,reference,title,state,actor,changed_at,reason,revision,correlation_id) VALUES('channel',?,?,'revoked','parent','now','parent_discard:language',1,'seed')",
            [(f'UC{index:022d}', f'Source {index}') for index in range(501)],
        )
    channel = f'UC{500:022d}'

    class Browser:
        async def cards_for_source(self, kind, reference):
            assert reference == HOME_SOURCE_REFERENCE
            return [{'channel_id': channel, 'channel_title': 'Discarded'}]

    classifier = FakeClassifier('SAFE')
    await ingest_once(db, Browser(), classifier, avatar_backfill_limit=0)
    assert classifier.calls == []
    assert (await db.catalog_channel_references()).count(channel) == 1
