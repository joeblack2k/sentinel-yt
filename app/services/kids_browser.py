from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import time
from pathlib import Path
from subprocess import DEVNULL, Popen, TimeoutExpired
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit
from urllib.request import urlopen

from websockets.sync.client import connect


KIDS_HOST = "www.youtubekids.com"
DEFAULT_PROFILE_PATH = Path("/opt/youtube-sub-browser/data/youtube-web-profile")
DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = 9223
DEFAULT_DISPLAY = ":99"
DEFAULT_VNC_PORT = 5901


class BrowserStartupError(RuntimeError):
    pass


def chromium_executable() -> str | None:
    for name in (
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
    ):
        path = shutil.which(name)
        if path:
            return path
    return None


def require_existing_profile(profile_dir: str | Path) -> Path:
    path = Path(profile_dir)
    if not path.is_dir():
        raise BrowserStartupError(
            f"Chromium profile must be an existing directory: {path}"
        )
    try:
        next(path.iterdir())
    except StopIteration:
        raise BrowserStartupError(
            f"Chromium profile must be a non-empty existing directory: {path}"
        ) from None
    except OSError as error:
        raise BrowserStartupError(f"Chromium profile cannot be read: {path}") from error
    return path


def clear_stale_chromium_singleton_locks(profile_dir: str | Path) -> None:
    profile = Path(profile_dir)
    lock = profile / "SingletonLock"
    singleton_paths = list(profile.glob("Singleton*"))
    if not singleton_paths:
        return
    if not os.path.lexists(lock) or not lock.is_symlink():
        raise BrowserStartupError("Chromium profile lock owner cannot be verified")
    try:
        owner, pid_text = os.readlink(lock).rsplit("-", 1)
        pid = int(pid_text)
    except (OSError, ValueError):
        raise BrowserStartupError("Chromium profile lock owner cannot be verified") from None
    if owner != socket.gethostname() or pid <= 1:
        raise BrowserStartupError("Chromium profile lock owner cannot be verified")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    except PermissionError:
        raise BrowserStartupError("Chromium profile is in use") from None
    else:
        raise BrowserStartupError("Chromium profile is in use")

    for path in singleton_paths:
        if path.is_symlink() or path.is_file():
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def port_in_use(host: str, port: int, *, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ensure_runtime_ports_free(vnc_port: int, debug_port: int) -> None:
    if port_in_use(DEFAULT_CDP_HOST, debug_port):
        raise BrowserStartupError(
            f"CDP port {DEFAULT_CDP_HOST}:{debug_port} is already in use"
        )
    if port_in_use(DEFAULT_CDP_HOST, vnc_port):
        raise BrowserStartupError(
            f"VNC port {DEFAULT_CDP_HOST}:{vnc_port} is already in use"
        )


def xvfb_command(display: str) -> list[str]:
    return [
        "Xvfb",
        display,
        "-screen",
        "0",
        "1600x900x24",
        "-nolisten",
        "tcp",
    ]


def x11vnc_command(display: str, vnc_port: int) -> list[str]:
    return [
        "x11vnc",
        "-display",
        display,
        "-localhost",
        "-nopw",
        "-shared",
        "-forever",
        "-rfbport",
        str(vnc_port),
    ]


def chromium_command(
    executable: str,
    profile_dir: str | Path,
    *,
    debug_port: int = DEFAULT_CDP_PORT,
) -> list[str]:
    command = [
        executable,
        "--app=https://www.youtubekids.com/",
        "--disable-dev-shm-usage",
        "--password-store=basic",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        # Restore both the app tab state and session cookies after a clean exit.
        "--restore-last-session",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--disable-blink-features=AutomationControlled",
        f"--remote-debugging-address={DEFAULT_CDP_HOST}",
        f"--remote-debugging-port={debug_port}",
        f"--remote-allow-origins=http://{DEFAULT_CDP_HOST}:{debug_port}",
        "--window-size=1600,900",
        "--window-position=0,0",
        f"--user-data-dir={Path(profile_dir)}",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        command.insert(1, "--no-sandbox")
    return command


def cdp_targets(
    debug_port: int = DEFAULT_CDP_PORT,
    *,
    opener: Callable[..., Any] = urlopen,
) -> list[dict[str, Any]]:
    with opener(
        f"http://{DEFAULT_CDP_HOST}:{debug_port}/json/list", timeout=0.75
    ) as response:
        payload = json.load(response)
    if not isinstance(payload, list):
        raise BrowserStartupError("CDP target list is not a JSON array")
    return [item for item in payload if isinstance(item, dict)]


def close_chromium_browser(
    debug_port: int = DEFAULT_CDP_PORT,
    *,
    opener: Callable[..., Any] = urlopen,
    connector: Callable[..., Any] = connect,
) -> None:
    with opener(
        f"http://{DEFAULT_CDP_HOST}:{debug_port}/json/version", timeout=0.75
    ) as response:
        version = json.load(response)
    _page_command(
        {"webSocketDebuggerUrl": version.get("webSocketDebuggerUrl")},
        "Browser.close",
        connector=connector,
    )


def page_targets(targets: Iterable[object]) -> list[dict[str, Any]]:
    return [
        item
        for item in targets
        if isinstance(item, dict) and item.get("type") == "page"
    ]


def is_kids_page_target(target: object) -> bool:
    if not isinstance(target, dict) or target.get("type") != "page":
        return False
    parsed = urlsplit(str(target.get("url", "")))
    return parsed.scheme == "https" and parsed.hostname == KIDS_HOST


def is_parent_auth_target(target: object) -> bool:
    if not isinstance(target, dict) or target.get("type") != "page":
        return False
    parsed = urlsplit(str(target.get("url", "")))
    return parsed.scheme == "https" and parsed.hostname == "accounts.google.com"


def _page_target_status(pages: list[dict[str, Any]]) -> dict[str, Any]:
    kids_pages = [target for target in pages if is_kids_page_target(target)]
    auth_pages = [target for target in pages if is_parent_auth_target(target)]
    return {
        "page_count": len(pages),
        "kids_page_count": len(kids_pages),
        "parent_auth_page_count": len(auth_pages),
        "ready": len(kids_pages) == 1
        and len(pages) == len(kids_pages) + len(auth_pages),
    }


def browser_target_status(targets: Iterable[object]) -> dict[str, Any]:
    return _page_target_status(page_targets(targets))


def validate_kids_targets(targets: Iterable[object]) -> dict[str, Any]:
    pages = page_targets(targets)
    status = _page_target_status(pages)
    if not status["ready"]:
        raise BrowserStartupError(
            "Chromium must expose one YouTube Kids page and only parent-auth pages"
        )
    return next(target for target in pages if is_kids_page_target(target))


def _page_command(
    target: dict[str, Any],
    method: str,
    params: dict[str, Any] | None = None,
    *,
    connector: Callable[..., Any] = connect,
) -> dict[str, Any]:
    websocket_url = str(target.get("webSocketDebuggerUrl", ""))
    if not websocket_url:
        raise BrowserStartupError("YouTube Kids page is not connectable")
    with connector(websocket_url, open_timeout=2, close_timeout=1) as websocket:
        websocket.send(
            json.dumps({"id": 1, "method": method, "params": params or {}})
        )
        while True:
            response = json.loads(websocket.recv(timeout=3))
            if response.get("id") == 1:
                if "error" in response:
                    raise BrowserStartupError(f"CDP {method} failed")
                return response.get("result", {})


def wait_for_kids_page(
    debug_port: int,
    *,
    processes: Iterable[Any] = (),
    timeout: float = 30,
    poll_seconds: float = 0.25,
    targets_reader: Callable[[int], list[dict[str, Any]]] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
) -> dict[str, Any]:
    reader = targets_reader or cdp_targets
    sleeper = sleep_fn or time.sleep
    monotonic = monotonic_fn or time.monotonic
    process_list = tuple(processes)
    deadline = monotonic() + max(0, timeout)
    last_error: Exception | None = None
    while True:
        if any(process.poll() is not None for process in process_list):
            raise BrowserStartupError("A browser process stopped during startup")
        try:
            return validate_kids_targets(reader(debug_port))
        except (BrowserStartupError, OSError, ValueError, TypeError) as error:
            last_error = error
        if monotonic() >= deadline:
            detail = f": {last_error}" if last_error else ""
            raise BrowserStartupError(
                f"Chromium did not become ready with one YouTube Kids page{detail}"
            ) from last_error
        sleeper(min(poll_seconds, max(0, deadline - monotonic())))


def _stop_process(process: Any) -> None:
    if process.poll() is not None:
        return
    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 1:
        with contextlib.suppress(OSError):
            os.killpg(pid, signal.SIGTERM)
    with contextlib.suppress(Exception):
        process.terminate()


def stop_browser_processes(processes: Iterable[Any]) -> None:
    running = tuple(process for process in processes if process is not None)
    for process in reversed(running):
        _stop_process(process)
    for process in reversed(running):
        if process.poll() is None:
            try:
                process.wait(timeout=3)
            except TimeoutExpired:
                pid = getattr(process, "pid", None)
                if isinstance(pid, int) and pid > 1:
                    with contextlib.suppress(OSError):
                        os.killpg(pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    process.kill()
                with contextlib.suppress(Exception):
                    process.wait(timeout=3)


def _wait_for_process(
    process: Any,
    name: str,
    *,
    sleep_fn: Callable[[float], None],
) -> None:
    sleep_fn(0.5)
    if process.poll() is not None:
        raise BrowserStartupError(f"{name} stopped immediately")


def launch_browser_runtime(
    *,
    display: str = DEFAULT_DISPLAY,
    vnc_port: int = DEFAULT_VNC_PORT,
    debug_port: int = DEFAULT_CDP_PORT,
    profile_dir: str | Path = DEFAULT_PROFILE_PATH,
    startup_timeout: float = 30,
    popen_factory: Callable[..., Any] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    targets_reader: Callable[[int], list[dict[str, Any]]] | None = None,
) -> tuple[Any, Any, Any]:
    profile = require_existing_profile(profile_dir)
    _ensure_runtime_ports_free(vnc_port, debug_port)
    executable = chromium_executable()
    if executable is None:
        raise BrowserStartupError("Chromium is not installed")

    # Lock cleanup is deliberately after the live-port check.
    clear_stale_chromium_singleton_locks(profile)
    popen = popen_factory or Popen
    sleeper = sleep_fn or time.sleep
    processes: list[Any] = []
    try:
        processes.append(
            popen(
                xvfb_command(display),
                stdout=DEVNULL,
                stderr=DEVNULL,
                start_new_session=True,
            )
        )
        _wait_for_process(processes[-1], "Xvfb", sleep_fn=sleeper)
        processes.append(
            popen(
                x11vnc_command(display, vnc_port),
                stdout=DEVNULL,
                stderr=DEVNULL,
                start_new_session=True,
            )
        )
        _wait_for_process(processes[-1], "x11vnc", sleep_fn=sleeper)
        environment = os.environ.copy()
        environment["DISPLAY"] = display
        processes.append(
            popen(
                chromium_command(executable, profile, debug_port=debug_port),
                env=environment,
                stdout=DEVNULL,
                stderr=DEVNULL,
                start_new_session=True,
            )
        )
        _wait_for_process(processes[-1], "Chromium", sleep_fn=sleeper)
        wait_for_kids_page(
            debug_port,
            processes=processes,
            timeout=startup_timeout,
            targets_reader=targets_reader,
            sleep_fn=sleeper,
        )
        return tuple(processes)  # type: ignore[return-value]
    except Exception:
        stop_browser_processes(processes)
        raise


def main() -> None:
    display = os.environ.get("KIDS_BROWSER_DISPLAY", DEFAULT_DISPLAY)
    vnc_port = int(os.environ.get("KIDS_BROWSER_VNC_PORT", str(DEFAULT_VNC_PORT)))
    debug_port = int(os.environ.get("KIDS_BROWSER_CDP_PORT", str(DEFAULT_CDP_PORT)))
    profile_dir = Path(
        os.environ.get("KIDS_BROWSER_PROFILE_PATH", str(DEFAULT_PROFILE_PATH))
    )
    stopped = False

    def request_stop(*_: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    processes = launch_browser_runtime(
        display=display,
        vnc_port=vnc_port,
        debug_port=debug_port,
        profile_dir=profile_dir,
    )
    try:
        invalid_target_checks = 0
        while not stopped:
            time.sleep(1)
            if any(process.poll() is not None for process in processes):
                raise BrowserStartupError(
                    "A browser process stopped unexpectedly"
                )
            try:
                validate_kids_targets(cdp_targets(debug_port))
                invalid_target_checks = 0
            except (BrowserStartupError, OSError, ValueError, TypeError):
                invalid_target_checks += 1
                if invalid_target_checks >= 5:
                    raise BrowserStartupError(
                        "Chromium no longer exposes one YouTube Kids page "
                        "and only parent-auth pages"
                    )
    finally:
        with contextlib.suppress(Exception):
            close_chromium_browser(debug_port)
        with contextlib.suppress(TimeoutExpired):
            processes[-1].wait(timeout=5)
        stop_browser_processes(processes)


if __name__ == "__main__":
    main()
