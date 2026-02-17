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


def test_api_sponsorblock_and_blocklists(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SENTINEL_PORT", "8091")
    module = importlib.import_module("app.main")
    module = importlib.reload(module)

    with TestClient(module.app) as client:
        sb = client.post("/api/sponsorblock/state", json={"active": True})
        assert sb.status_code == 200
        assert sb.json()["active"] is True

        save_local = client.post(
            "/api/rules/blocklists/local",
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
                "mode": "whitelist",
            },
        )
        assert add_schedule.status_code == 200


def test_api_manual_pair_validation_message(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SENTINEL_PORT", "8092")
    module = importlib.import_module("app.main")
    module = importlib.reload(module)

    with TestClient(module.app) as client:
        resp = client.post("/api/devices/pair/code", json={"pairing_code": "123"})
        assert resp.status_code == 422
        payload = resp.json()
        assert payload["detail"]["code"] == "validation_error"
        assert "at least 4 characters" in payload["detail"]["message"]
