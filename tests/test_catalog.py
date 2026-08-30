import importlib

from fastapi.testclient import TestClient


def test_catalog_default_deny_transitions_and_listing(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_DB_PATH", str(tmp_path / "sentinel.db"))
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SENTINEL_KIDS_PARENT_TOKEN", "parent-test")
    monkeypatch.setenv("SENTINEL_KIDS_INGEST_TOKEN", "ingest-test")
    monkeypatch.setenv("SENTINEL_KIDS_GATEWAY_TOKEN", "gateway-test")
    module = importlib.reload(importlib.import_module("app.main"))

    with TestClient(module.app) as client:
        source = client.post(
            "/api/kids/sources",
            headers={
                "X-Correlation-ID": "c-source",
                "Authorization": "Bearer parent-test",
            },
            json={"kind": "channel", "reference": "UC-test", "title": "Test channel"},
        )
        assert source.status_code == 200
        source_data = source.json()
        assert source_data["state"] == "candidate"
        assert source_data["revision"] == 1

        item = client.post(
            "/api/kids/catalog/items",
            headers={
                "X-Correlation-ID": "c-item",
                "Authorization": "Bearer ingest-test",
            },
            json={"video_id": "video-1", "title": "Safe candidate", "source_id": source_data["id"]},
        )
        assert item.status_code == 200
        item_data = item.json()
        assert item_data["state"] == "candidate"
        gateway_headers = {"Authorization": "Bearer gateway-test"}
        assert client.get("/api/kids/catalog/items", headers=gateway_headers).json()["items"] == []
        assert client.get(
            "/api/kids/catalog/items/by-video/video-1",
            headers=gateway_headers,
        ).json()["state"] == "candidate"

        approved_source = client.patch(
            f"/api/kids/sources/{source_data['id']}/state",
            headers={"Authorization": "Bearer parent-test"},
            json={
                "state": "approved",
                "actor": "parent",
                "reason": "reviewed",
                "correlation_id": "t1",
            },
        )
        assert approved_source.json()["state"] == "approved"
        assert client.get("/api/kids/catalog/items", headers=gateway_headers).json()["items"] == []

        approved_item = client.patch(
            f"/api/kids/catalog/items/{item_data['id']}/state",
            headers={"Authorization": "Bearer parent-test"},
            json={
                "state": "approved",
                "actor": "parent",
                "reason": "reviewed",
                "correlation_id": "t2",
            },
        )
        assert approved_item.json()["state"] == "approved"
        assert [
            x["video_id"]
            for x in client.get("/api/kids/catalog/items", headers=gateway_headers).json()["items"]
        ] == ["video-1"]
        assert client.get(
            "/api/kids/catalog/revision",
            headers=gateway_headers,
        ).json()["revision"] == 4

        revoked = client.patch(
            f"/api/kids/catalog/items/{item_data['id']}/state",
            headers={"Authorization": "Bearer parent-test"},
            json={
                "state": "revoked",
                "actor": "parent",
                "reason": "withdrawn",
                "correlation_id": "t3",
            },
        )
        assert revoked.json()["state"] == "revoked"
        assert client.get("/api/kids/catalog/items", headers=gateway_headers).json()["items"] == []
        reapprove = client.patch(
            f"/api/kids/catalog/items/{item_data['id']}/state",
            headers={"Authorization": "Bearer parent-test"},
            json={
                "state": "approved",
                "actor": "parent",
                "reason": "again",
                "correlation_id": "t4",
            },
        )
        assert reapprove.status_code == 409
