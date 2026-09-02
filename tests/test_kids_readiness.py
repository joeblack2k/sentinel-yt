import asyncio
import importlib
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import kids_api
from app.db import Database


class FakeClassifier:
    available = True

    def __init__(self, *, base_url, model):
        self.model = model

    async def check_model(self):
        if not self.available:
            raise RuntimeError("test OpenCodex outage")
        return self.model

    async def close(self):
        return None


def test_kids_readyz_requires_fresh_ingest_and_opencodex(tmp_path, monkeypatch):
    db_path = str(tmp_path / "sentinel.db")
    monkeypatch.setenv("SENTINEL_DB_PATH", db_path)
    monkeypatch.setenv("SENTINEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KIDS_INGEST_FRESHNESS_SECONDS", "1800")
    monkeypatch.setenv("KIDS_READY_MINIMUM", "0")
    module = importlib.reload(importlib.import_module("app.main"))
    monkeypatch.setattr(kids_api, "OpenCodexKidsClassifier", FakeClassifier)

    with TestClient(module.app) as client:
        assert client.get("/api/kids/readyz").status_code == 503
        assert client.get("/readyz").status_code == 503

    asyncio.run(
        Database(db_path).set_setting(
            "kids_ingest_last_success_at",
            datetime.now(timezone.utc).isoformat(),
        )
    )
    with TestClient(module.app) as client:
        ready = client.get("/api/kids/readyz")
        assert ready.status_code == 200
        assert ready.json()["opencodex"] == "ready"
        assert ready.json()["ingest"] == "fresh"
        assert client.get("/readyz").status_code == 200

    FakeClassifier.available = False
    with TestClient(module.app) as client:
        unavailable = client.get("/api/kids/readyz")
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"]["opencodex"] == "unavailable"
