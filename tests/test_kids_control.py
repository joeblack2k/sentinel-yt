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
