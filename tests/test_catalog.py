import importlib

from fastapi.testclient import TestClient


def test_catalog_default_deny_transitions_and_listing(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        source = client.post(
            "/api/kids/sources",
            headers={"X-Correlation-ID": "c-source"},
            json={"kind": "channel", "reference": "UC-test", "title": "Test channel"},
        )
        assert source.status_code == 200
        source_data = source.json()
        assert source_data["state"] == "candidate"
        assert source_data["revision"] == 1

        item = client.post(
            "/api/kids/catalog/items",
            headers={"X-Correlation-ID": "c-item"},
            json={"video_id": "video-1", "title": "Safe candidate", "source_id": source_data["id"]},
        )
        assert item.status_code == 200
        item_data = item.json()
        assert item_data["state"] == "candidate"
        assert client.get("/api/kids/catalog/items").json()["items"] == []
        assert client.get(
            "/api/kids/catalog/items/by-video/video-1",
        ).json()["state"] == "candidate"

        approved_source = client.patch(
            f"/api/kids/sources/{source_data['id']}/state",
            json={
                "state": "approved",
                "actor": "parent",
                "reason": "reviewed",
                "correlation_id": "t1",
            },
        )
        assert approved_source.json()["state"] == "approved"
        assert client.get("/api/kids/catalog/items").json()["items"] == []

        approved_item = client.patch(
            f"/api/kids/catalog/items/{item_data['id']}/state",
            json={
                "state": "approved",
                "actor": "parent",
                "reason": "reviewed",
                "correlation_id": "t2",
            },
        )
        assert approved_item.json()["state"] == "approved"
        assert client.get("/api/kids/catalog/items").json()["items"] == []
        assert client.get(
            "/api/kids/catalog/revision",
        ).json()["revision"] == 4

        revoked = client.patch(
            f"/api/kids/catalog/items/{item_data['id']}/state",
            json={
                "state": "revoked",
                "actor": "parent",
                "reason": "withdrawn",
                "correlation_id": "t3",
            },
        )
        assert revoked.json()["state"] == "revoked"
        assert client.get("/api/kids/catalog/items").json()["items"] == []
        reapprove = client.patch(
            f"/api/kids/catalog/items/{item_data['id']}/state",
            json={
                "state": "approved",
                "actor": "parent",
                "reason": "again",
                "correlation_id": "t4",
            },
        )
        assert reapprove.status_code == 409
