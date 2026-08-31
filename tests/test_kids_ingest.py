import asyncio
import json
import sqlite3
from dataclasses import dataclass

import pytest

from app.config import Settings
from app.db import Database
from app.services.blocklists import BlocklistService
from app.services.judge import JudgeService
from app.services.kids_ingest import (
    HOME_SOURCE_REFERENCE,
    YouTubeKidsCDP,
    ingest_once,
    parse_cards,
    source_url,
)
from app.services.webhook import WebhookClient


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


@pytest.mark.asyncio
async def test_cdp_reuses_existing_kids_page_without_creating_or_closing_target(monkeypatch):
    import app.services.kids_ingest as kids_ingest

    commands: list[dict] = []

    class FakeWebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, message):
            commands.append(json.loads(message))

        async def recv(self):
            command = commands[-1]
            if command["method"] == "Runtime.evaluate":
                return json.dumps(
                    {
                        "id": command["id"],
                        "result": {
                            "result": {
                                "value": json.dumps(
                                    {
                                        "url": "https://www.youtubekids.com/",
                                        "ready": "complete",
                                        "cards": [{"href": "/watch?v=abcdefghijk"}],
                                    }
                                )
                            }
                        },
                    }
                )
            return json.dumps({"id": command["id"], "result": {}})

    monkeypatch.setattr(
        kids_ingest.websockets,
        "connect",
        lambda url, **kwargs: FakeWebSocket(),
    )
    adapter = YouTubeKidsCDP(wait_seconds=0.5)
    json_list_calls = 0

    async def fake_json(path):
        nonlocal json_list_calls
        assert path == "/json/list"
        json_list_calls += 1
        return [
            {
                "id": "target-1",
                "type": "page",
                "url": "https://www.youtubekids.com/",
                "webSocketDebuggerUrl": "ws://page",
            },
        ]

    adapter._json = fake_json

    assert await adapter.cards_for_source("channel", HOME_SOURCE_REFERENCE) == [
        {"href": "/watch?v=abcdefghijk"}
    ]
    assert await adapter.cards_for_source("channel", "UC123") == [
        {"href": "/watch?v=abcdefghijk"}
    ]
    assert [command["method"] for command in commands] == [
        "Runtime.enable",
        "Page.navigate",
        "Runtime.evaluate",
        "Runtime.enable",
        "Page.navigate",
        "Runtime.evaluate",
    ]
    assert commands[1]["params"] == {"url": "https://www.youtubekids.com/"}
    assert commands[4]["params"] == {"url": "https://www.youtubekids.com/channel/UC123"}
    assert json_list_calls == 2
    assert all(
        command["method"] not in {"Target.createTarget", "Target.closeTarget"}
        for command in commands
    )


@pytest.mark.asyncio
async def test_cdp_rejects_navigation_away_without_closing_existing_target(monkeypatch):
    import app.services.kids_ingest as kids_ingest

    commands: list[dict] = []

    class FakeWebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, message):
            commands.append(json.loads(message))

        async def recv(self):
            command = commands[-1]
            if command["method"] == "Runtime.evaluate":
                return json.dumps(
                    {
                        "id": command["id"],
                        "result": {
                            "result": {
                                "value": json.dumps(
                                    {
                                        "url": "https://www.youtube.com/",
                                        "ready": "complete",
                                        "cards": [],
                                    }
                                )
                            }
                        },
                    }
                )
            return json.dumps({"id": command["id"], "result": {}})

    monkeypatch.setattr(
        kids_ingest.websockets,
        "connect",
        lambda url, **kwargs: FakeWebSocket(),
    )
    adapter = YouTubeKidsCDP(wait_seconds=0.5)

    async def fake_json(path):
        assert path == "/json/list"
        return [
            {
                "id": "target-1",
                "type": "page",
                "url": "https://www.youtubekids.com/",
                "webSocketDebuggerUrl": "ws://page",
            },
        ]

    adapter._json = fake_json

    with pytest.raises(RuntimeError, match="left YouTube Kids"):
        await adapter.cards_for_source("channel", HOME_SOURCE_REFERENCE)
    assert [command["method"] for command in commands] == [
        "Runtime.enable",
        "Page.navigate",
        "Runtime.evaluate",
    ]


@pytest.mark.asyncio
async def test_cdp_does_not_create_a_new_target_when_existing_kids_page_is_missing():
    adapter = YouTubeKidsCDP(wait_seconds=0.5)

    async def empty_targets(path):
        assert path == "/json/list"
        return []

    adapter._json = empty_targets

    with pytest.raises(RuntimeError, match="No existing YouTube Kids CDP target"):
        await adapter.cards_for_source("channel", HOME_SOURCE_REFERENCE)


@pytest.mark.asyncio
async def test_cdp_command_timeout_covers_unsolicited_messages():
    class FakeWebSocket:
        async def send(self, message):
            return None

        async def recv(self):
            return json.dumps({"method": "Runtime.consoleAPICalled"})

    with pytest.raises(asyncio.TimeoutError):
        await YouTubeKidsCDP()._command(
            FakeWebSocket(),
            1,
            "Runtime.evaluate",
            timeout=0.01,
        )


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
class ChannelHomeBrowser:
    async def cards_for_source(self, kind, reference):
        if reference == HOME_SOURCE_REFERENCE:
            return [
                {
                    "href": "/watch?v=abcdefghijk",
                    "title": "Safe home animals",
                    "label": "Safe home animals by Approved Channel 10 views",
                    "duration": "5:00",
                    "thumbnail_url": "https://i.ytimg.com/vi/abcdefghijk/hqdefault.jpg",
                    "channel_id": "UC123",
                }
            ]
        if reference == "UC123":
            return [
                {
                    "href": "/watch?v=zyxwvutsrqp",
                    "title": "Safe channel animals",
                    "label": "Safe channel animals by Approved Channel 10 views",
                    "duration": "4:00",
                    "thumbnail_url": "https://i.ytimg.com/vi/zyxwvutsrqp/hqdefault.jpg",
                    "channel_id": "UC123",
                }
            ]
        return []


@dataclass
class BlocklistedBrowser:
    async def cards_for_source(self, kind, reference):
        if reference == HOME_SOURCE_REFERENCE:
            return [
                {
                    "href": "/watch?v=deny0000001",
                    "title": "Blocked test item",
                    "label": "Blocked test item by Approved Channel 10 views",
                    "duration": "5:00",
                    "thumbnail_url": "https://i.ytimg.com/vi/deny0000001/hqdefault.jpg",
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
    assert classifier.calls[0]["sample_videos"][0]["video_id"] == "abcdefghijk"
    items = await db.catalog_items_list()
    assert {item["video_id"] for item in items} == {"abcdefghijk"}
    assert items[0]["source_id"] == source["id"]
    assert items[0]["channel_id"] == "UC123"
    assert items[0]["channel_title"] == "Kids Channel"
    checked_source = await db.catalog_get("source", source["id"])
    assert checked_source["safety_policy_version"] == "sampled-channel-v1"
    assert checked_source["safety_sample_count"] == 1
    assert json.loads(checked_source["safety_evidence_json"])[0]["video_id"] == "abcdefghijk"

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


@pytest.mark.asyncio
async def test_safe_channel_stays_hidden_until_parent_approval(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    source = await db.catalog_create(
        "source",
        {
            "kind": "channel",
            "reference": "UC123",
            "title": "Discovered channel",
            "correlation_id": "source",
        },
    )
    await db.catalog_source_safety_update(
        source["id"],
        verdict="SAFE",
        reason="test",
        actor="kids-channel-guardian",
        correlation_id="classify-source",
    )

    first = await ingest_once(db, ChannelHomeBrowser(), FakeClassifier("SAFE"))
    assert first.approved == 0
    assert await db.catalog_items_list() == []

    await db.catalog_transition(
        "source",
        source["id"],
        {
            "state": "approved",
            "actor": "parent",
            "reason": "approved for Noah",
            "correlation_id": "approve-source",
        },
    )
    cached_classifier = FakeClassifier("SAFE")
    second = await ingest_once(db, ChannelHomeBrowser(), cached_classifier)
    assert second.approved == 2
    assert cached_classifier.calls == []
    assert [item["video_id"] for item in await db.catalog_items_list()] == [
        "abcdefghijk",
        "zyxwvutsrqp",
    ]
    assert {item["source_id"] for item in await db.catalog_items_list()} == {source["id"]}


@pytest.mark.asyncio
async def test_ingest_honors_local_blocklist_and_demotes_existing_approved_item(tmp_path):
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
    item = await db.catalog_create(
        "item",
        {
            "video_id": "deny0000001",
            "title": "Blocked test item",
            "source_id": source["id"],
            "channel_id": "UC123",
            "channel_title": "Approved Channel",
            "correlation_id": "item",
        },
    )
    await db.catalog_transition(
        "item",
        item["id"],
        {
            "state": "approved",
            "actor": "parent",
            "reason": "legacy approval",
            "correlation_id": "approve-item",
        },
    )

    settings = Settings(
        db_path=str(tmp_path / "sentinel.db"),
        data_dir=str(tmp_path / "data"),
    )
    blocklists = BlocklistService(settings)
    await blocklists.save_local_content("video:deny0000001 | configured block\n")
    await blocklists.reload(db)
    judge = JudgeService(db, settings, WebhookClient(), blocklists=blocklists)
    classifier = FakeClassifier("SAFE")
    report = await ingest_once(db, BlocklistedBrowser(), classifier, judge=judge)

    assert report.blocked == 1
    assert (await db.catalog_item_by_video("deny0000001"))["state"] == "blocked"
    assert await db.catalog_items_list() == []


@pytest.mark.asyncio
async def test_catalog_item_channel_identity_columns_migrate(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE catalog_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                source_id INTEGER REFERENCES catalog_sources(id),
                thumbnail_url TEXT NOT NULL DEFAULT '',
                duration_seconds INTEGER NOT NULL DEFAULT 0,
                visual_category TEXT NOT NULL DEFAULT 'general',
                state TEXT NOT NULL DEFAULT 'candidate',
                actor TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                revision INTEGER NOT NULL,
                correlation_id TEXT NOT NULL
            )
            """
        )
        connection.commit()

    db = Database(str(db_path))
    await db.init()
    item = await db.catalog_create(
        "item",
        {
            "video_id": "legacy-item",
            "title": "Legacy item",
            "correlation_id": "legacy-item",
        },
    )

    assert item["channel_id"] == ""
    assert item["channel_title"] == ""
    with sqlite3.connect(db_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(catalog_items)")}
    assert {"channel_id", "channel_title"} <= columns
