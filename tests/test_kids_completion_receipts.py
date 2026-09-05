import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from tests.test_kids_resolver import eligible_item
from tests.test_kids_dataplane import seed_catalog, load_app, mock_upstream


@pytest.mark.asyncio
async def test_concurrent_completion_retries_share_receipt(tmp_path):
    db = Database(str(tmp_path / "kids.db"))
    await db.init()
    item = await eligible_item(db, "completion-retry")
    payload = dict(
        video_id=item["video_id"], event="completed", profile="noah",
        position_seconds=None, session_id="same-session", startup_ms=None,
        correlation_id="completion-retry",
    )
    receipts = await asyncio.gather(*(db.kids_watch_event_record(**payload) for _ in range(4)))
    assert len({receipt["id"] for receipt in receipts}) == 1
    other = await db.kids_watch_event_record(**{**payload, "profile": "felix"})
    assert other["id"] != receipts[0]["id"]
    another = await db.kids_watch_event_record(**{**payload, "session_id": "next-session"})
    assert another["id"] != receipts[0]["id"]


def test_committed_receipt_survives_closed_lease_but_cannot_create_history(tmp_path, monkeypatch):
    asyncio.run(seed_catalog(tmp_path / "sentinel.db", qualities=(1080,)))
    module = load_app(tmp_path, monkeypatch)
    mock_upstream(monkeypatch, module, [])
    with TestClient(module.app) as client:
        asset = client.get("/v1/kids/feed").json()["items"][0]["id"]
        lease = client.post("/v1/kids/playback-sessions", json={"asset_id": asset}).json()["id"]
        different_asset = client.get("/v1/kids/feed").json()["items"][0]["id"]
        payload = dict(asset_id=asset, session_id=lease, profile="noah", event="completed")
        first = client.post("/v1/kids/events", json=payload)
        assert first.status_code == 202
        client.delete(f"/v1/kids/playback-sessions/{lease}")
        retry = client.post("/v1/kids/events", json=payload)
        assert retry.status_code == 202
        assert retry.json() == first.json()
        assert client.post("/v1/kids/events", json={**payload, "profile": "felix"}).status_code != 202
        assert different_asset != asset
        assert client.post("/v1/kids/events", json={**payload, "asset_id": different_asset}).status_code != 202
        other = client.post("/v1/kids/playback-sessions", json={"asset_id": asset}).json()["id"]
        client.delete(f"/v1/kids/playback-sessions/{other}")
        assert client.post("/v1/kids/events", json={**payload, "session_id": other}).status_code != 202
        with sqlite3.connect(tmp_path / "sentinel.db") as connection:
            connection.execute("UPDATE feed_sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        expired_retry = client.post("/v1/kids/events", json=payload)
        assert expired_retry.status_code == 202
        assert expired_retry.json() == first.json()
