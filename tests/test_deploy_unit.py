"""Slice 12 (`auto-start-deployment`): S7 ("starts by itself after a power
cut") and S8 ("boot ordering never blocks on the internet").

**Be honest about what this file can and cannot prove.** S7's own literal
criterion is pulling the plug on a Raspberry Pi. Nothing here does that --
there is no Pi. What is proven instead: the shipped unit file's own
directives (12.1), and a killed-process-then-relaunched proxy (12.3) that
exercises the process-level half of what `Restart=always` depends on
systemd to do in production. Both are named as proxies in
`specs/station-deployment/spec.md`'s own "Hardware gate" notes, not as the
hardware gate itself.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

UNIT_PATH = Path(__file__).resolve().parents[1] / "deploy" / "feeder-station.service"


def _parse_unit_file(path: Path) -> dict[str, list[str]]:
    """A deliberately tiny reader -- just enough to pull `key=value`
    directives out of an INI-shaped systemd unit file. This project has no
    other reason to parse this format, so no general parser is warranted.
    """
    directives: dict[str, list[str]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "[")):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        directives.setdefault(key.strip(), []).append(value.strip())
    return directives


# --- 12.1: the unit file's own directives -----------------------------------


def test_unit_file_has_restart_always_and_no_internet_wait_dependency() -> None:
    """S7: `Restart=always` is what makes a crash, kill, or power cut
    self-healing. S8: nothing in `Requires=`, `Wants=`, or `After=` may
    name `network-online.target` -- this site's internet is intermittent
    (docs/requirements.md), and the station must never wait on it to start
    serving `/health` and the operator UI.
    """
    directives = _parse_unit_file(UNIT_PATH)

    assert "always" in directives.get("Restart", [])
    assert any("network.target" in value for value in directives.get("After", []))

    for key in ("Requires", "Wants", "After"):
        for value in directives.get(key, []):
            assert "network-online.target" not in value


def test_unit_file_is_image_agnostic() -> None:
    """The runbook (`docs/deploy.md`), not the unit file, is where
    image-specific detail belongs (`chrony` vs `ntp`, `/boot/firmware` vs
    `/boot`) -- this slice's own instruction, echoing design.md section 8's
    identical split for the export folder's destination vs the runbook.
    """
    text = UNIT_PATH.read_text(encoding="utf-8")

    assert "/boot/firmware" not in text
    assert "chrony" not in text
    assert "ntpd" not in text


# --- 12.3: process-level restart proxy (CI proxy, not the hardware gate) ---


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(port: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
            if response.status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    return False


def _spawn(
    *, port: int, db_path: Path, extra_args: list[str] | None = None
) -> subprocess.Popen[bytes]:
    args = [
        sys.executable,
        "-m",
        "feeder_station.cli",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--db-path",
        str(db_path),
        "--no-gateway",  # no real hardware in CI
        *(extra_args or []),
    ]
    return subprocess.Popen(args)


def test_process_restarts_after_being_killed(tmp_path: Path) -> None:
    """CI proxy, not S7's literal hardware gate (a power-pull on a
    physical Pi) -- see this slice's own tasks.md and
    specs/station-deployment/spec.md's "Hardware gate" note. What this
    proves: `cli.py` exits cleanly enough under SIGKILL that a fresh
    process against the *same* `station.db` comes back up and answers
    `/health` -- the process-level half of what `Restart=always` (12.1/
    12.2) depends on systemd to do in production.
    """
    port = _free_port()
    db_path = tmp_path / "station.db"

    first = _spawn(port=port, db_path=db_path)
    try:
        assert _wait_for_health(port, timeout=10.0), "first launch never answered /health"
        first.kill()
        first.wait(timeout=5)
    finally:
        if first.poll() is None:
            first.kill()
            first.wait(timeout=5)

    # Relaunching itself is systemd's job (`Restart=always`, proven by
    # 12.1's unit-file inspection) -- this proves the process-level half: a
    # fresh process against the same on-disk store comes up clean.
    second = _spawn(port=port, db_path=db_path)
    try:
        assert _wait_for_health(port, timeout=10.0), "relaunch never answered /health"
    finally:
        second.kill()
        second.wait(timeout=5)


# --- 12.5/12.6: no startup path requires an internet route ------------------


def test_service_starts_with_no_route_to_the_internet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S8. Rather than simulate "no internet" indirectly (a real offline
    Pi is not available here), this blocks the one primitive every path to
    an outbound connection has to go through -- ``socket.socket.connect``
    -- and proves the full startup path (real lifespan, real worker
    threads, a real request to `/health` and `/`) never calls it. FastAPI's
    `TestClient` talks ASGI in-process, with no real socket involved in the
    request/response cycle itself, so this is a clean proxy for "nothing
    here needs a network route" rather than a simulation of a specific
    offline network stack.
    """
    import socket

    from fastapi.testclient import TestClient

    from feeder_station.adapters.serial_gateway import DeviceConfig, SerialGateway
    from feeder_station.adapters.store import Store
    from feeder_station.app import create_app

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("network access attempted -- S8 forbids this at startup")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)

    store = Store(tmp_path / "station.db")
    gateway = SerialGateway(DeviceConfig())  # no device resolves -- link stays down
    app = create_app(store=store, gateway=gateway)

    with TestClient(app) as client:
        deadline = time.monotonic() + 2.0
        body: dict[str, object] = {}
        while time.monotonic() < deadline:
            body = client.get("/health").json()
            workers = body.get("workers")
            if isinstance(workers, dict) and workers.get("gateway", {}).get("alive"):
                break
            time.sleep(0.02)

        assert client.get("/").status_code == 200
        assert body.get("workers", {}).get("gateway", {}).get("alive") is True
        assert body.get("workers", {}).get("gateway", {}).get("last_error") is None
