import importlib

from fastapi.testclient import TestClient


def test_kids_routes_require_scoped_runtime_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SENTINEL_KIDS_PARENT_TOKEN", "parent-test")
    monkeypatch.setenv("SENTINEL_KIDS_INGEST_TOKEN", "ingest-test")
    monkeypatch.setenv("SENTINEL_KIDS_GATEWAY_TOKEN", "gateway-test")
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        assert client.get("/api/kids/status").status_code == 401
        assert client.get(
            "/api/kids/status",
            headers={"Authorization": "Bearer parent-test"},
        ).status_code == 403
        assert client.get(
            "/api/kids/status",
            headers={"Authorization": "Bearer gateway-test"},
        ).status_code == 200

        source = client.post(
            "/api/kids/sources",
            headers={"Authorization": "Bearer parent-test"},
            json={"kind": "channel", "reference": "UC-auth", "title": "Auth"},
        )
        assert source.status_code == 200
        audit = client.get(
            "/api/kids/audit",
            headers={"Authorization": "Bearer parent-test"},
        )
        assert audit.status_code == 200
        assert any(event["event"] == "candidate_created" for event in audit.json()["events"])
        assert all("token" not in str(event).lower() for event in audit.json()["events"])

        enabled = client.post(
            "/api/kids/control/kill-switch",
            headers={"Authorization": "Bearer parent-test"},
            json={
                "enabled": True,
                "actor": "parent",
                "reason": "maintenance",
                "correlation_id": "kill-1",
            },
        )
        assert enabled.status_code == 200
        assert client.get(
            "/api/kids/catalog/items",
            headers={"Authorization": "Bearer gateway-test"},
        ).json() == {"state": "kill_switch", "items": []}

        disabled = client.post(
            "/api/kids/control/kill-switch",
            headers={"Authorization": "Bearer parent-test"},
            json={
                "enabled": False,
                "actor": "parent",
                "reason": "ready",
                "correlation_id": "kill-2",
            },
        )
        assert disabled.status_code == 200
