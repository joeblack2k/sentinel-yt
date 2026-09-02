import importlib
import os

from fastapi.testclient import TestClient


def test_api_status_and_control(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SENTINEL_PORT", "8090")
    module = importlib.import_module("app.main")
    module = importlib.reload(module)

    with TestClient(module.app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200

        status = client.get("/api/status")
        assert status.status_code == 200
        assert "active" in status.json()

        control = client.post("/api/control/state", json={"active": False})
        assert control.status_code == 200
        assert control.json()["active"] is False
        status_after = client.get("/api/status")
        assert status_after.status_code == 200
        assert status_after.json()["active"] is False


def test_api_kids_guardian_surfaces(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SENTINEL_PORT", "8091")
    module = importlib.import_module("app.main")
    module = importlib.reload(module)

    with TestClient(module.app) as client:
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 307
        assert root.headers["location"] == "/kids"
        for path in ("/live", "/allowlist", "/devices", "/automation", "/mqtt", "/sponsorblock", "/rules"):
            assert client.get(path).status_code == 404

        for path in (
            "/api/sponsorblock/state",
            "/api/mqtt/state",
            "/api/devices/pair/code",
            "/api/live/events",
            "/api/rules/whitelist",
        ):
            assert client.post(path, json={"active": True}).status_code == 404

        save_local = client.post(
            "/api/blocklist/local",
            json={
                "content": (
                    "# test\n"
                    "video:dQw4w9WgXcQ | test video | https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"
                )
            },
        )
        assert save_local.status_code == 200
        assert save_local.json()["summary"]["video_count"] >= 1

        stats = client.get("/api/db/stats")
        assert stats.status_code == 200
        assert "total_bytes" in stats.json()

        schedules = client.get("/api/schedules")
        assert schedules.status_code == 200
        assert schedules.json()["count"] >= 1

        add_schedule = client.post(
            "/api/schedules/add",
            json={
                "name": "Evening whitelist",
                "enabled": True,
                "start": "18:00",
                "end": "21:00",
                "timezone": "UTC",
                "mode": "blocklist",
            },
        )
        assert add_schedule.status_code == 200

        history = client.get("/api/history")
        assert history.status_code == 200
        assert history.json()["kids_watch_events"] == []


def test_api_removed_device_surface_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SENTINEL_PORT", "8092")
    module = importlib.import_module("app.main")
    module = importlib.reload(module)

    with TestClient(module.app) as client:
        resp = client.post("/api/devices/pair/code", json={"pairing_code": "123"})
        assert resp.status_code == 404
