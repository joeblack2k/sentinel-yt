import importlib

from fastapi.testclient import TestClient


def test_kids_routes_are_local_lan_open_and_control_is_audited(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        assert client.get("/api/kids/status").status_code == 200

        source = client.post(
            "/api/kids/sources",
            json={"kind": "channel", "reference": "UC-local", "title": "Local"},
        )
        assert source.status_code == 200

        enabled = client.post(
            "/api/kids/control/kill-switch",
            json={
                "enabled": True,
                "actor": "parent",
                "reason": "maintenance",
                "correlation_id": "kill-1",
            },
        )
        assert enabled.status_code == 200
        assert client.get("/api/kids/catalog/items").json() == {
            "state": "kill_switch",
            "items": [],
        }

        audit = client.get("/api/kids/audit").json()["events"]
        assert audit[0]["event"] == "kill_switch_changed"
        assert audit[1]["event"] == "candidate_created"

        disabled = client.post(
            "/api/kids/control/kill-switch",
            json={
                "enabled": False,
                "actor": "parent",
                "reason": "ready",
                "correlation_id": "kill-2",
            },
        )
        assert disabled.status_code == 200
        assert client.get("/api/kids/status").json()["kill_switch"] is False


def test_approved_item_requires_approved_source(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        orphan = client.post(
            "/api/kids/catalog/items",
            json={"video_id": "orphan", "title": "No source"},
        ).json()
        assert client.patch(
            f"/api/kids/catalog/items/{orphan['id']}/state",
            json={
                "state": "approved",
                "actor": "parent",
                "reason": "reviewed",
                "correlation_id": "orphan-approve",
            },
        ).status_code == 200
    assert client.get("/api/kids/catalog/items").json()["items"] == []


def test_parent_kids_page_is_available(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        response = client.get("/kids")
        assert response.status_code == 200
        assert "SubTube Kids" in response.text
        assert "Kids sources" in response.text
        assert "Guardian check" in response.text

        source = client.post(
            "/api/kids/sources",
            json={"kind": "channel", "reference": "UC-revoked"},
        ).json()
        item = client.post(
            "/api/kids/catalog/items",
            json={"video_id": "revoked-source", "source_id": source["id"]},
        ).json()
        for path, body in (
            (
                f"/api/kids/sources/{source['id']}/state",
                {"state": "approved", "actor": "parent", "reason": "ok", "correlation_id": "source-ok"},
            ),
            (
                f"/api/kids/catalog/items/{item['id']}/state",
                {"state": "approved", "actor": "parent", "reason": "ok", "correlation_id": "item-ok"},
            ),
            (
                f"/api/kids/sources/{source['id']}/state",
                {"state": "revoked", "actor": "parent", "reason": "withdrawn", "correlation_id": "source-revoked"},
            ),
        ):
            assert client.patch(path, json=body).status_code == 200
        assert client.get("/api/kids/catalog/items").json()["items"] == []


def test_kids_watch_events_are_persisted_and_unknown_videos_are_rejected(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        source = client.post(
            "/api/kids/sources",
            json={"kind": "channel", "reference": "UC-watch", "title": "Watch"},
        ).json()
        item = client.post(
            "/api/kids/catalog/items",
            json={
                "video_id": "video-watch",
                "title": "Watch item",
                "source_id": source["id"],
                "visual_category": "animals",
            },
        ).json()
        for path, body in (
            (
                f"/api/kids/sources/{source['id']}/state",
                {
                    "state": "approved",
                    "actor": "parent",
                    "reason": "trusted channel",
                    "correlation_id": "watch-source",
                },
            ),
            (
                f"/api/kids/catalog/items/{item['id']}/state",
                {
                    "state": "approved",
                    "actor": "parent",
                    "reason": "trusted item",
                    "correlation_id": "watch-item",
                },
            ),
        ):
            assert client.patch(path, json=body).status_code == 200

        accepted = client.post(
            "/api/kids/watch-events",
            json={
                "video_id": "video-watch",
                "event": "completed",
                "profile": "noah",
                "position_seconds": 95.5,
                "session_id": "play-1",
                "startup_ms": 1450,
                "correlation_id": "watch-1",
            },
        )
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "accepted"

        events = client.get("/api/kids/watch-events").json()["events"]
        assert events[0]["video_id"] == "video-watch"
        assert events[0]["startup_ms"] == 1450
        assert events[0]["event"] == "completed"
        assert events[0]["profile"] == "noah"
        assert events[0]["position_seconds"] == 95.5
        assert events[0]["source_title"] == "Watch"

        unknown = client.post(
            "/api/kids/watch-events",
            json={
                "video_id": "not-in-catalog",
                "event": "selected",
                "correlation_id": "watch-unknown",
            },
        )
        assert unknown.status_code == 404
