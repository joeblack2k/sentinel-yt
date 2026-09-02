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
    KidsBrowserSetupRequired,
    YouTubeKidsCDP,
    ingest_once,
    parse_cards,
    source_url,
)


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
                navigation = next(
                    command
                    for command in reversed(commands)
                    if command["method"] == "Page.navigate"
                )
                return json.dumps(
                    {
                        "id": command["id"],
                        "result": {
                            "result": {
                                "value": json.dumps(
                                        {
                                            "url": navigation["params"]["url"],
                                            "ready": "complete",
                                            "scroll_y": 0,
                                            "scroll_height": 0,
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
            {
                "id": "account-1",
                "type": "page",
                "url": "https://accounts.google.com/signin",
                "webSocketDebuggerUrl": "ws://account",
            },
        ]

    adapter._json = fake_json

    assert await adapter.cards_for_source("channel", HOME_SOURCE_REFERENCE) == [
        {"href": "/watch?v=abcdefghijk"}
    ]
    assert await adapter.cards_for_source("channel", "UC123") == [
        {"href": "/watch?v=abcdefghijk"}
    ]
    navigate_urls = [
        command["params"]["url"]
        for command in commands
        if command["method"] == "Page.navigate"
    ]
    assert navigate_urls[0] == "https://www.youtubekids.com/"
    assert navigate_urls[-1] == "https://www.youtubekids.com/"
    assert "https://www.youtubekids.com/channel/UC123" in navigate_urls
    assert [
        f"https://www.youtubekids.com/search?q={term}"
        for term in kids_ingest._SEARCH_TERMS
    ] == navigate_urls[1 : 1 + len(kids_ingest._SEARCH_TERMS)]
    assert json_list_calls == 2
    assert adapter._target_id == "target-1"
    assert all(
        command["method"] not in {"Target.createTarget", "Target.closeTarget"}
        for command in commands
    )


@pytest.mark.asyncio
async def test_cdp_home_budget_preserves_category_and_language_search_cards(monkeypatch):
    import app.services.kids_ingest as kids_ingest

    commands: list[dict] = []
    home_url = "https://www.youtubekids.com/"
    category_url = "https://www.youtubekids.com/category/animals"
    home_cards = [{"href": f"/watch?v=h{index:010d}"} for index in range(160)]

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
                navigation = next(
                    command
                    for command in reversed(commands)
                    if command["method"] == "Page.navigate"
                )
                url = navigation["params"]["url"]
                if url == home_url:
                    payload = {
                        "url": url,
                        "ready": "complete",
                        "scroll_y": 0,
                        "scroll_height": 0,
                        "cards": home_cards,
                        "category_urls": [category_url],
                    }
                elif url == category_url:
                    payload = {
                        "url": url,
                        "ready": "complete",
                        "scroll_y": 0,
                        "scroll_height": 0,
                        "cards": [{"href": "/watch?v=c0000000001"}],
                    }
                elif "q=dieren" in url:
                    payload = {
                        "url": url,
                        "ready": "complete",
                        "scroll_y": 0,
                        "scroll_height": 0,
                        "cards": [{"href": "/watch?v=n0000000001"}],
                    }
                elif "q=animals" in url:
                    payload = {
                        "url": url,
                        "ready": "complete",
                        "scroll_y": 0,
                        "scroll_height": 0,
                        "cards": [{"href": "/watch?v=e0000000001"}],
                    }
                else:
                    payload = {
                        "url": url,
                        "ready": "complete",
                        "scroll_y": 0,
                        "scroll_height": 0,
                        "cards": [],
                    }
                return json.dumps(
                    {
                        "id": command["id"],
                        "result": {"result": {"value": json.dumps(payload)}},
                    }
                )
            return json.dumps({"id": command["id"], "result": {}})

    monkeypatch.setattr(
        kids_ingest.websockets,
        "connect",
        lambda url, **kwargs: FakeWebSocket(),
    )
    adapter = YouTubeKidsCDP(wait_seconds=0.2)

    async def fake_json(path):
        assert path == "/json/list"
        return [
            {
                "id": "target-1",
                "type": "page",
                "url": home_url,
                "webSocketDebuggerUrl": "ws://page",
            }
        ]

    adapter._json = fake_json

    cards = await adapter.cards_for_source("channel", HOME_SOURCE_REFERENCE)
    card_ids = {
        item["href"].split("v=", 1)[1]
        for item in cards
        if "v=" in item.get("href", "")
    }
    navigate_urls = [
        command["params"]["url"]
        for command in commands
        if command["method"] == "Page.navigate"
    ]

    assert len(cards) == 43
    assert {"c0000000001", "n0000000001", "e0000000001"} <= card_ids
    assert navigate_urls[-1] == home_url
    assert len(cards) <= kids_ingest._MAX_COLLECTED_CARDS


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
    navigate_urls = [
        command["params"]["url"]
        for command in commands
        if command["method"] == "Page.navigate"
    ]
    assert navigate_urls[0] == "https://www.youtubekids.com/"
    assert navigate_urls[-1] == "https://www.youtubekids.com/"
    assert all(
        command["method"] not in {"Target.createTarget", "Target.closeTarget"}
        for command in commands
    )


@pytest.mark.asyncio
async def test_cdp_reports_parent_setup_before_scrolling():
    import app.services.kids_ingest as kids_ingest

    adapter = YouTubeKidsCDP(wait_seconds=1)
    calls: list[str] = []

    async def fake_command(websocket, request_id, method, params=None, *, timeout=None):
        calls.append(method)
        if method == "Runtime.evaluate":
            return {
                "result": {
                    "value": json.dumps(
                        {
                            "url": "https://www.youtubekids.com/",
                            "ready": "complete",
                            "setup_required": True,
                            "cards": [],
                        }
                    )
                }
            }
        return {}

    adapter._command = fake_command

    with pytest.raises(kids_ingest.KidsBrowserSetupRequired):
        await adapter._collect_route(
            object(),
            "https://www.youtubekids.com/",
            {},
        )

    assert calls == ["Page.navigate", "Runtime.evaluate"]


@pytest.mark.asyncio
async def test_cdp_does_not_restore_home_after_parent_setup_abort(monkeypatch):
    import app.services.kids_ingest as kids_ingest

    commands: list[dict] = []
    home_url = "https://www.youtubekids.com/"

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
                                        "url": home_url,
                                        "ready": "complete",
                                        "setup_required": True,
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
    adapter = kids_ingest.YouTubeKidsCDP(wait_seconds=0.5)

    async def fake_json(path):
        assert path == "/json/list"
        return [
            {
                "id": "target-1",
                "type": "page",
                "url": home_url,
                "webSocketDebuggerUrl": "ws://page",
            }
        ]

    adapter._json = fake_json

    with pytest.raises(kids_ingest.KidsBrowserSetupRequired):
        await adapter.cards_for_source("channel", HOME_SOURCE_REFERENCE)

    navigate_urls = [
        command["params"]["url"]
        for command in commands
        if command["method"] == "Page.navigate"
    ]
    assert navigate_urls == [home_url]
    assert adapter._setup_required
    await adapter.restore_home()
    assert not adapter._setup_required
    navigate_urls = [
        command["params"]["url"]
        for command in commands
        if command["method"] == "Page.navigate"
    ]
    assert navigate_urls == [home_url]


@pytest.mark.asyncio
async def test_cdp_allows_transient_external_redirect_only_until_kids_returns(monkeypatch):
    import app.services.kids_ingest as kids_ingest

    commands: list[dict] = []
    evaluation_index = 0

    class FakeWebSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, message):
            commands.append(json.loads(message))

        async def recv(self):
            nonlocal evaluation_index
            command = commands[-1]
            if command["method"] == "Runtime.evaluate":
                navigation = next(
                    command
                    for command in reversed(commands)
                    if command["method"] == "Page.navigate"
                )
                payload = (
                    {
                        "url": "https://www.youtube.com/",
                        "ready": "complete",
                        "cards": [{"href": "/watch?v=must-not-be-read"}],
                    }
                    if evaluation_index == 0
                    else {
                        "url": navigation["params"]["url"],
                        "ready": "complete",
                        "scroll_y": 0,
                        "scroll_height": 0,
                        "cards": [{"href": "/watch?v=abcdefghijk"}],
                    }
                )
                evaluation_index += 1
                return json.dumps(
                    {
                        "id": command["id"],
                        "result": {
                            "result": {
                                "value": json.dumps(payload),
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
    adapter = YouTubeKidsCDP(wait_seconds=2.0)

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

    assert await adapter.cards_for_source("channel", HOME_SOURCE_REFERENCE) == [
        {"href": "/watch?v=abcdefghijk"}
    ]
    assert all(
        command["method"] not in {"Target.createTarget", "Target.closeTarget"}
        for command in commands
    )
    assert all(
        "youtube.com" not in command.get("params", {}).get("url", "")
        for command in commands
        if command["method"] == "Page.navigate"
    )


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
@pytest.mark.parametrize(
    "extra_page",
    [
        {
            "id": "second-kids",
            "type": "page",
            "url": "https://www.youtubekids.com/channel/UC123",
            "webSocketDebuggerUrl": "ws://second-kids",
        },
        {
            "id": "youtube",
            "type": "page",
            "url": "https://www.youtube.com/",
            "webSocketDebuggerUrl": "ws://youtube",
        },
    ],
)
async def test_cdp_rejects_multiple_or_non_kids_pages(extra_page):
    adapter = YouTubeKidsCDP()

    async def invalid_targets(path):
        assert path == "/json/list"
        return [
            {
                "id": "target-1",
                "type": "page",
                "url": "https://www.youtubekids.com/",
                "webSocketDebuggerUrl": "ws://page",
            },
            extra_page,
        ]

    adapter._json = invalid_targets

    with pytest.raises(RuntimeError, match="exactly one YouTube Kids page"):
        await adapter._reusable_target()


@pytest.mark.asyncio
async def test_cdp_keeps_using_the_persistent_target_id():
    adapter = YouTubeKidsCDP()
    adapter._target_id = "target-1"

    async def changed_target(path):
        assert path == "/json/list"
        return [
            {
                "id": "target-2",
                "type": "page",
                "url": "https://www.youtubekids.com/",
                "webSocketDebuggerUrl": "ws://page",
            }
        ]

    adapter._json = changed_target

    with pytest.raises(RuntimeError, match="Persistent.*changed"):
        await adapter._reusable_target()


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


@pytest.mark.asyncio
async def test_ingest_does_not_scan_channels_when_parent_setup_is_required(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()

    class SetupBrowser:
        references: list[str] = []

        async def cards_for_source(self, kind, reference):
            self.references.append(reference)
            if reference == HOME_SOURCE_REFERENCE:
                raise KidsBrowserSetupRequired("setup")
            raise AssertionError("channel scan must wait for parent setup")

    browser = SetupBrowser()
    report = await ingest_once(db, browser, FakeClassifier("SAFE"))

    assert report.errors == 1
    assert browser.references == [HOME_SOURCE_REFERENCE]


@pytest.mark.asyncio
async def test_ingest_rotates_source_batches_between_runs(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    channel_ids = [f"UC{index:022d}" for index in range(1, 4)]
    for channel_id in channel_ids:
        source = await db.catalog_create(
            "source",
            {
                "kind": "channel",
                "reference": channel_id,
                "title": f"Channel {channel_id[-1]}",
                "correlation_id": f"source-{channel_id}",
            },
        )
        await db.catalog_source_safety_update(
            source["id"],
            verdict="SAFE",
            reason="test",
            actor="kids-channel-guardian",
            correlation_id=f"classify-{channel_id}",
        )
        await db.catalog_transition(
            "source",
            source["id"],
            {
                "state": "approved",
                "actor": "parent",
                "reason": "test",
                "correlation_id": f"approve-{channel_id}",
            },
        )

    class RotatingBrowser:
        references: list[str] = []

        async def cards_for_source(self, kind, reference):
            self.references.append(reference)
            if reference == HOME_SOURCE_REFERENCE:
                return []
            return [
                {
                    "href": f"/watch?v={reference[-11:]}",
                    "title": f"Video {reference[-1]}",
                    "label": f"Video by Channel {reference[-1]} 10 views",
                    "duration": "5:00",
                    "thumbnail_url": f"https://i.ytimg.com/vi/{reference[-11:]}/hqdefault.jpg",
                    "channel_id": reference,
                }
            ]

    browser = RotatingBrowser()
    first = await ingest_once(
        db,
        browser,
        FakeClassifier("SAFE"),
        source_batch_size=2,
    )
    second = await ingest_once(
        db,
        browser,
        FakeClassifier("SAFE"),
        source_batch_size=2,
    )

    assert first.sources_seen == 4
    assert second.sources_seen == 4
    assert browser.references == [
        HOME_SOURCE_REFERENCE,
        channel_ids[0],
        channel_ids[1],
        HOME_SOURCE_REFERENCE,
        channel_ids[2],
        channel_ids[0],
    ]
    assert await db.get_setting("kids_ingest_source_offset") == "1"
    assert first.approved == 2
    assert second.approved == 1


@pytest.mark.asyncio
async def test_ingest_keeps_source_offset_when_batch_aborts(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    channel_ids = [f"UC{index:022d}" for index in range(1, 4)]
    for channel_id in channel_ids:
        await db.catalog_create(
            "source",
            {
                "kind": "channel",
                "reference": channel_id,
                "title": f"Channel {channel_id[-1]}",
                "correlation_id": f"source-{channel_id}",
            },
        )
    await db.set_setting("kids_ingest_source_offset", "1")

    class FailingBrowser:
        references: list[str] = []

        async def cards_for_source(self, kind, reference):
            self.references.append(reference)
            if reference == HOME_SOURCE_REFERENCE:
                return []
            raise KidsBrowserSetupRequired("setup")

    browser = FailingBrowser()
    report = await ingest_once(
        db,
        browser,
        FakeClassifier("SAFE"),
        source_batch_size=2,
    )

    assert report.errors == 1
    assert browser.references == [HOME_SOURCE_REFERENCE, channel_ids[1]]
    assert await db.get_setting("kids_ingest_source_offset") == "1"


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
    language: str = "unknown"
    calls: list[dict] = None

    def __post_init__(self):
        self.calls = []

    async def classify(self, metadata):
        self.calls.append(metadata)
        return {"verdict": self.verdict, "language": self.language, "reason": "test"}


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
    classifier = FakeClassifier("SAFE", "en")
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
    assert checked_source["language"] == "en"
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
async def test_safe_new_channel_auto_approves_and_publishes_only_approved_source_items(tmp_path):
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

    classifier = FakeClassifier("SAFE", "nl")
    first = await ingest_once(db, ChannelHomeBrowser(), classifier)
    assert first.approved == 2
    assert classifier.calls
    assert (await db.catalog_get("source", source["id"]))["state"] == "approved"
    assert (await db.catalog_get("source", source["id"]))["language"] == "nl"
    assert [item["video_id"] for item in await db.catalog_items_list()] == [
        "abcdefghijk",
        "zyxwvutsrqp",
    ]
    assert {item["source_id"] for item in await db.catalog_items_list()} == {source["id"]}

    cached_classifier = FakeClassifier("SAFE")
    second = await ingest_once(db, ChannelHomeBrowser(), cached_classifier)
    assert second.approved == 0
    assert second.skipped == 2
    assert cached_classifier.calls == []


@pytest.mark.asyncio
async def test_uncertain_or_unknown_source_stays_hidden(tmp_path):
    db = Database(str(tmp_path / "sentinel.db"))
    await db.init()
    source = await db.catalog_create(
        "source",
        {
            "kind": "channel",
            "reference": "UC123",
            "title": "Uncertain channel",
            "correlation_id": "source",
        },
    )

    report = await ingest_once(db, ChannelHomeBrowser(), FakeClassifier("UNKNOWN", "nl"))

    assert report.approved == 0
    assert report.uncertain == 1
    stored = await db.catalog_get("source", source["id"])
    assert stored["state"] == "candidate"
    assert stored["safety_verdict"] == "UNCERTAIN"
    assert stored["language"] == "nl"
    assert await db.catalog_items_list() == []


@pytest.mark.asyncio
async def test_safe_new_source_blocked_by_judge_does_not_auto_approve(tmp_path):
    db_path = tmp_path / "sentinel.db"
    db = Database(str(db_path))
    await db.init()
    channel_id = "UC" + "a" * 22
    source = await db.catalog_create(
        "source",
        {
            "kind": "channel",
            "reference": channel_id,
            "title": "Blocked discovered channel",
            "correlation_id": "source",
        },
    )
    settings = Settings(db_path=str(db_path), data_dir=str(tmp_path / "data"))
    blocklists = BlocklistService(settings)
    await blocklists.save_local_content(f"channel:{channel_id} | configured block\n")
    await blocklists.reload(db)
    judge = JudgeService(db, blocklists=blocklists)

    classifier = FakeClassifier("SAFE")
    report = await ingest_once(
        db,
        ChannelHomeBrowser(),
        classifier,
        judge=judge,
    )

    assert report.blocked == 1
    assert classifier.calls == []
    assert (await db.catalog_get("source", source["id"]))["state"] == "blocked"
    assert await db.catalog_items_list() == []


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
    judge = JudgeService(db, blocklists=blocklists)
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
