from dataclasses import dataclass

import pytest

from app.db import Database
from app.services.kids_ingest import ingest_once, parse_cards, source_url


def test_source_url_stays_inside_youtube_kids():
    assert source_url("channel", "UC123") == "https://www.youtubekids.com/channel/UC123"
    assert source_url("playlist", "PL123") == "https://www.youtubekids.com/playlist?list=PL123"
    assert source_url("channel", "https://www.youtubekids.com/channel/UC123").endswith("/UC123")


def test_parse_cards_rejects_shorts_bad_host_and_missing_duration():
    cards = [
        {
            "href": "/watch?v=abcdefghijk",
            "title": "Safe",
            "label": "Safe by Channel 10 views",
            "duration": "12:34",
            "thumbnail_url": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg?x=1",
        },
        {
            "href": "/shorts/abcdefghijk",
            "title": "Short",
            "duration": "0:30",
            "thumbnail_url": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
        },
        {
            "href": "/watch?v=bad",
            "title": "No",
            "duration": "12:34",
            "thumbnail_url": "https://i.ytimg.com/vi/bad/hqdefault.jpg",
        },
        {
            "href": "/watch?v=zyxwvutsrqp",
            "title": "No duration",
            "thumbnail_url": "https://i.ytimg.com/vi/zyxwvutsrqp/hqdefault.jpg",
        },
        {
            "href": "/watch?v=qwertyuiopa",
            "title": "Wrong image host",
            "duration": "12:34",
            "thumbnail_url": "https://example.test/thumb.jpg",
        },
    ]
    parsed = parse_cards(cards, source_id=1, source_reference="UC123")
    assert [(item.video_id, item.duration_seconds, item.channel_title) for item in parsed] == [
        ("abcdefghijk", 754, "Channel")
    ]


@dataclass
class FakeBrowser:
    async def cards_for_source(self, kind, reference):
        return [
            {
                "href": "/watch?v=abcdefghijk",
                "title": "Safe animals",
                "label": "Safe animals by Approved Channel 10 views",
                "duration": "5:00",
                "thumbnail_url": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
            }
        ]


@dataclass
class FakeClassifier:
    verdict: str

    async def classify(self, metadata):
        return {"verdict": self.verdict, "reason": "test"}


@pytest.mark.asyncio
async def test_ingest_only_publishes_safe_decisions(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    source = await db.catalog_create(
        "source",
        {
            "kind": "channel",
            "reference": "UC123",
            "title": "Approved Channel",
            "correlation_id": "source",
        },
    )
    await db.catalog_transition(
        "source",
        source["id"],
        {
            "state": "approved",
            "actor": "parent",
            "reason": "test",
            "correlation_id": "approve-source",
        },
    )
    report = await ingest_once(db, FakeBrowser(), FakeClassifier("SAFE"))
    assert report.approved == 1
    assert (await db.catalog_items_list())[0]["video_id"] == "abcdefghijk"

    await db.catalog_transition(
        "item",
        1,
        {
            "state": "revoked",
            "actor": "parent",
            "reason": "test revoke",
            "correlation_id": "revoke",
        },
    )
    report = await ingest_once(db, FakeBrowser(), FakeClassifier("SAFE"))
    assert report.skipped == 1
    assert await db.catalog_items_list() == []
