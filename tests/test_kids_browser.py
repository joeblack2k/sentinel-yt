from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from app.services.kids_browser import (
    BrowserStartupError,
    browser_target_status,
    clear_stale_chromium_singleton_locks,
    chromium_command,
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
    assert "--app=https://www.youtubekids.com/" in command
    assert "--user-data-dir=/persistent/kids-profile" in command
    assert "--remote-debugging-port=9223" in command
    assert "www.youtube.com" not in rendered


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

    assert "app.services.kids_browser" in browser_unit
    assert "/opt/sentinel-yt/current" in browser_unit
    assert (
        "KIDS_BROWSER_PROFILE_PATH=/opt/youtube-sub-browser/data/youtube-web-profile"
        in browser_unit
    )
    assert "subtube-kids-guardian" not in browser_unit
    assert "Requires=sentinel-yt-kids-browser.service" in novnc_unit
