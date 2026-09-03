from __future__ import annotations

import io
import json
import os
import socket
import sqlite3
from pathlib import Path

import pytest

from app.services.kids_browser import (
    BrowserStartupError,
    browser_target_status,
    checkpoint_kids_session_cookies,
    clear_stale_chromium_singleton_locks,
    close_chromium_browser,
    chromium_command,
    enable_chromium_session_restore,
    open_kids_page_if_missing,
    persist_kids_session_cookies,
    require_existing_profile,
)


def test_browser_reuses_only_an_existing_nonempty_profile(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(BrowserStartupError):
        require_existing_profile(missing)

    profile = tmp_path / "profile"
    profile.mkdir()
    with pytest.raises(BrowserStartupError):
        require_existing_profile(profile)

    (profile / "Default").mkdir()
    assert require_existing_profile(profile) == profile


def test_browser_is_ready_only_with_one_youtube_kids_page() -> None:
    kids = {"type": "page", "url": "https://www.youtubekids.com/"}
    service_worker = {
        "type": "service_worker",
        "url": "https://www.youtubekids.com/sw.js",
    }
    assert browser_target_status([kids, service_worker]) == {
        "page_count": 1,
        "kids_page_count": 1,
        "parent_auth_page_count": 0,
        "ready": True,
    }
    assert not browser_target_status(
        [kids, {"type": "page", "url": "https://www.youtubekids.com/search"}]
    )["ready"]
    assert not browser_target_status(
        [{"type": "page", "url": "https://www.youtube.com/"}]
    )["ready"]


def test_browser_allows_only_google_parent_auth_beside_kids_page() -> None:
    kids = {"type": "page", "url": "https://www.youtubekids.com/"}
    parent_auth = {
        "type": "page",
        "url": "https://accounts.google.com/o/oauth2/auth",
    }
    status = browser_target_status([kids, parent_auth])
    assert status["ready"]
    assert status["parent_auth_page_count"] == 1
    assert not browser_target_status(
        [kids, {"type": "page", "url": "https://example.com/"}]
    )["ready"]


def test_chromium_command_uses_the_persistent_kids_profile_only() -> None:
    command = chromium_command("chromium", "/persistent/kids-profile")
    rendered = " ".join(command)
    assert "--app=" not in rendered
    assert "--user-data-dir=/persistent/kids-profile" in command
    assert "--restore-last-session" in command
    assert "--remote-debugging-port=9223" in command
    assert "www.youtube.com" not in rendered


def test_browser_enables_native_session_restore_atomically(tmp_path: Path) -> None:
    preferences = tmp_path / "Default" / "Preferences"
    preferences.parent.mkdir()
    preferences.write_text('{"profile":{"name":"Default"}}')

    enable_chromium_session_restore(tmp_path)

    assert json.loads(preferences.read_text()) == {
        "profile": {"name": "Default"},
        "session": {"restore_on_startup": 1},
    }
    assert not list(preferences.parent.glob(".Preferences.*"))


class FakeWebSocket:
    def __init__(self, result):
        self.result = result
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def send(self, message):
        self.sent.append(json.loads(message))

    def recv(self, *, timeout):
        return json.dumps({"id": 1, "result": self.result})


def test_browser_closes_chromium_through_its_browser_endpoint() -> None:
    requests = []
    websocket = FakeWebSocket({})

    def opener(url, *, timeout):
        requests.append((url, timeout))
        return io.BytesIO(b'{"webSocketDebuggerUrl":"ws://browser"}')

    close_chromium_browser(
        9223,
        opener=opener,
        connector=lambda *_, **__: websocket,
    )

    assert requests == [("http://127.0.0.1:9223/json/version", 0.75)]
    assert websocket.sent == [{"id": 1, "method": "Browser.close", "params": {}}]


def test_browser_persists_only_relevant_session_cookies() -> None:
    target = {
        "type": "page",
        "url": "https://www.youtubekids.com/",
        "webSocketDebuggerUrl": "ws://kids-page",
    }
    sockets = []
    results = [
        {
            "cookies": [
                {
                    "name": "google-session",
                    "value": "secret",
                    "domain": ".google.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "session": True,
                },
                {
                    "name": "already-persistent",
                    "value": "kept",
                    "domain": ".youtube.com",
                    "session": False,
                    "expires": 999,
                },
                {
                    "name": "unrelated",
                    "value": "ignored",
                    "domain": ".example.com",
                    "session": True,
                },
            ]
        },
        {"success": True},
    ]

    def connector(*_, **__):
        websocket = FakeWebSocket(results.pop(0))
        sockets.append(websocket)
        return websocket

    assert checkpoint_kids_session_cookies(
        targets_reader=lambda _: [target],
        connector=connector,
        now_fn=lambda: 1000,
    ) == 1
    assert [socket.sent[0]["method"] for socket in sockets] == [
        "Network.getAllCookies",
        "Network.setCookie",
    ]
    params = sockets[1].sent[0]["params"]
    assert params["name"] == "google-session"
    assert params["domain"] == ".google.com"
    assert params["expires"] == 1000 + 400 * 24 * 60 * 60
    assert "session" not in params


def test_browser_opens_kids_only_when_no_restorable_kids_page_exists() -> None:
    requests = []

    def opener(request, *, timeout):
        requests.append((request.get_method(), request.full_url, timeout))
        return io.BytesIO(b"{}")

    targets = [
        {"id": "new-tab", "type": "page", "url": "chrome://newtab/"},
        {
            "id": "parent-auth",
            "type": "page",
            "url": "https://accounts.google.com/signin",
        },
    ]
    assert open_kids_page_if_missing(9223, targets, opener=opener)
    assert requests == [
        ("PUT", "http://127.0.0.1:9223/json/close/new-tab", 0.75),
        ("PUT", "http://127.0.0.1:9223/json/new?https://www.youtubekids.com/", 0.75),
    ]
    requests.clear()
    assert not open_kids_page_if_missing(
        9223,
        [{"id": "kids", "type": "page", "url": "https://www.youtubekids.com/"}],
        opener=opener,
    )
    assert not requests


def test_browser_recovery_tolerates_an_already_closed_page() -> None:
    def opener(request, *, timeout):
        if "/json/close/" in request.full_url:
            raise OSError("target already closed")
        return io.BytesIO(b"{}")

    assert open_kids_page_if_missing(
        9223,
        [{"id": "gone", "type": "page", "url": "chrome://newtab/"}],
        opener=opener,
    )


def test_only_youtube_kids_session_cookies_become_persistent(tmp_path: Path) -> None:
    cookie_db = tmp_path / "Default" / "Cookies"
    cookie_db.parent.mkdir()
    with sqlite3.connect(cookie_db) as connection:
        connection.execute(
            """
            CREATE TABLE cookies (
                host_key TEXT,
                name TEXT,
                expires_utc INTEGER,
                has_expires INTEGER,
                is_persistent INTEGER
            )
            """
        )
        connection.executemany(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?)",
            [
                (".youtubekids.com", "YSC", 0, 0, 0),
                (".youtube.com", "YSC", 0, 0, 0),
                (".www.youtubekids.com", "LOGIN_INFO", 123, 1, 1),
            ],
        )

    persist_kids_session_cookies(tmp_path)

    with sqlite3.connect(cookie_db) as connection:
        rows = {
            (row[0], row[1]): row[2:]
            for row in connection.execute(
                "SELECT host_key, name, expires_utc, has_expires, is_persistent "
                "FROM cookies"
            )
        }
    assert rows[(".youtube.com", "YSC")] == (0, 0, 0)
    assert rows[(".www.youtubekids.com", "LOGIN_INFO")] == (123, 1, 1)
    assert rows[(".youtubekids.com", "YSC")][0] > 0
    assert rows[(".youtubekids.com", "YSC")][1:] == (1, 1)


def test_browser_never_clears_a_live_or_unverifiable_profile_lock(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    lock = profile / "SingletonLock"
    lock.symlink_to(f"{socket.gethostname()}-{os.getpid()}")

    with pytest.raises(BrowserStartupError, match="in use"):
        clear_stale_chromium_singleton_locks(profile)
    assert lock.is_symlink()

    lock.unlink()
    lock.symlink_to("other-host-123")
    with pytest.raises(BrowserStartupError, match="cannot be verified"):
        clear_stale_chromium_singleton_locks(profile)
    assert lock.is_symlink()


def test_browser_clears_only_a_verified_stale_profile_lock(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        target = (
            f"{socket.gethostname()}-999999999"
            if name == "SingletonLock"
            else "stale"
        )
        (profile / name).symlink_to(target)

    clear_stale_chromium_singleton_locks(profile)
    assert not any(profile.glob("Singleton*"))


def test_browser_never_clears_singletons_without_owner_lock(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    cookie = profile / "SingletonCookie"
    cookie.symlink_to("unknown")

    with pytest.raises(BrowserStartupError, match="cannot be verified"):
        clear_stale_chromium_singleton_locks(profile)
    assert cookie.is_symlink()


def test_sentinel_units_own_the_persistent_browser_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    browser_unit = (root / "deploy/sentinel-yt-kids-browser.service").read_text()
    novnc_unit = (root / "deploy/sentinel-yt-kids-novnc.service").read_text()
    ingest_unit = (root / "deploy/sentinel-yt-kids-ingest.service").read_text()
    resolver_unit = (root / "deploy/sentinel-yt-kids-resolver.service").read_text()

    assert "app.services.kids_browser" in browser_unit
    assert "/opt/sentinel-yt/current" in browser_unit
    assert "KillMode=mixed" in browser_unit
    assert (
        "KIDS_BROWSER_PROFILE_PATH=/opt/youtube-sub-browser/data/youtube-web-profile"
        in browser_unit
    )
    assert "subtube-kids-guardian" not in browser_unit
    assert "Requires=sentinel-yt-kids-browser.service" in novnc_unit
    assert "sentinel-yt-kids-browser.service" in ingest_unit
    assert "sentinel-yt-kids-browser.service" in resolver_unit
    assert "youtube-sub-browser" not in ingest_unit
    assert "youtube-sub-browser" not in resolver_unit
