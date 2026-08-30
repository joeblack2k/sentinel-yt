from dataclasses import dataclass

import pytest

from app.db import Database
from app.services.kids_ingest import HOME_SOURCE_REFERENCE, ingest_once, parse_cards, source_url


def test_source_url_stays_inside_youtube_kids():
    assert source_url("channel", "UC123") == "https://www.youtubekids.com/channel/UC123"
    assert source_url("playlist", "PL123") == "https://www.youtubekids.com/playlist?list=PL123"
    assert source_url("channel", "https://www.youtubekids.com/channel/UC123").endswith("/UC123")
    assert source_url("channel", HOME_SOURCE_REFERENCE) == "https://www.youtubekids.com/"
    with pytest.raises(ValueError):
        source_url("channel", "https://www.youtubekids.com/watch?v=abcdefghijk")
    with pytest.raises(ValueError):
        source_url("playlist", "https://www.youtubekids.com/search?query=lego")


def test_channel_reference_normalization_and_discovery_id_validation():
    from app.services.kids_ingest import _CHANNEL_ID, channel_id_from_reference

    channel_id = "UCefhjBQbDVq0oqpoFBLuJrg"
    assert _CHANNEL_ID.fullmatch(channel_id)
    assert channel_id_from_reference(
        f"https://www.youtubekids.com/channel/{channel_id}"
    ) == channel_id
    assert not _CHANNEL_ID.fullmatch("UC123")


def test_parse_cards_rejects_shorts_bad_host_and_missing_duration():
    cards = [
        {
            "href": "/watch?v=abcdefghijk",
            "title": "Safe",
            "label": "Safe by Channel 10 views",
            "duration": "12:34",
            "thumbnail_url": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg?x=1",
            "channel_id": "UC123",
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
    assert parse_cards(
        cards,
        source_id=1,
        source_reference=HOME_SOURCE_REFERENCE,
        allowed_channel_ids={"OTHER"},
    ) == []


@dataclass
class FakeBrowser:
    async def cards_for_source(self, kind, reference):
        if reference == HOME_SOURCE_REFERENCE:
            return [
                {
                    "href": "/watch?v=abcdefghijk",
                    "title": "Safe home animals",
                    "label": "Safe home animals by Kids Channel 10 views",
                    "duration": "5:00",
                    "thumbnail_url": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
                    "channel_id": "UC123",
                }
            ]
        return []


@dataclass
class FakeClassifier:
    verdict: str
    calls: list[dict] = None

    def __post_init__(self):
        self.calls = []

    async def classify(self, metadata):
        self.calls.append(metadata)
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
    classifier = FakeClassifier("SAFE")
    report = await ingest_once(db, FakeBrowser(), classifier)
    assert report.approved == 1
    assert len(classifier.calls) == 1
    assert classifier.calls[0]["channel_id"] == "UC123"
    assert {item["video_id"] for item in await db.catalog_items_list()} == {"abcdefghijk"}

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
