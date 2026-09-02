import importlib

from fastapi.testclient import TestClient


def test_history_contains_kids_watch_events(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        source = client.post(
            "/api/kids/sources",
            json={"kind": "channel", "reference": "UC-history", "title": "History source"},
        ).json()
        item = client.post(
            "/api/kids/catalog/items",
            json={"video_id": "history-video", "title": "History video", "source_id": source["id"]},
        ).json()
        for path, correlation_id in (
            (f"/api/kids/sources/{source['id']}/state", "history-source"),
            (f"/api/kids/catalog/items/{item['id']}/state", "history-item"),
        ):
            assert client.patch(
                path,
                json={
                    "state": "approved",
                    "actor": "parent",
                    "reason": "history test",
                    "correlation_id": correlation_id,
                },
            ).status_code == 200

        assert client.post(
            "/api/kids/watch-events",
            json={
                "video_id": "history-video",
                "event": "completed",
                "profile": "noah",
                "position_seconds": 42,
                "startup_ms": 900,
                "session_id": "history-session",
                "correlation_id": "history-watch",
            },
        ).status_code == 202

        page = client.get("/history")
        assert page.status_code == 200
        assert "Kids watch history" in page.text
        assert "History video" in page.text

        api = client.get("/api/history")
        assert api.status_code == 200
        assert api.json()["kids_watch_events"][0]["video_id"] == "history-video"
