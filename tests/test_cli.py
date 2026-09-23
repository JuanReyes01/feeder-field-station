"""``cli.py`` — 12.8: the args a systemd-managed deployment needs to point
at real on-disk state and a real (or deliberately absent) serial device,
plus the rollback kill switch (`--no-gateway`) design.md names as the
concurrency section's own escape hatch: "the gateway is off by default...
turned on by cli.py when a device is configured."

``build_app`` is what `main()` calls before handing off to `uvicorn.run` --
pulled out so these tests can construct the real app and inspect what each
flag produced without actually starting a server.
"""

from __future__ import annotations

from pathlib import Path

from feeder_station.cli import build_app
from feeder_station.config import StationConfig


def test_db_path_flag_is_used_to_open_the_store(tmp_path: Path) -> None:
    db_path = tmp_path / "custom-station.db"

    app, args = build_app(["--db-path", str(db_path), "--no-gateway"])

    assert args.db_path == str(db_path)
    assert app.state.store is not None
    assert db_path.exists()  # Store.__init__ creates the file on open


def test_no_gateway_flag_starts_with_no_gateway_configured(tmp_path: Path) -> None:
    app, args = build_app(["--db-path", str(tmp_path / "station.db"), "--no-gateway"])

    assert args.no_gateway is True
    assert app.state.gateway is None


def test_serial_device_flag_is_passed_through_to_the_gateway(tmp_path: Path) -> None:
    app, args = build_app(
        [
            "--db-path",
            str(tmp_path / "station.db"),
            "--serial-device",
            "/dev/ttyUSB0",
        ]
    )

    assert args.no_gateway is False
    assert app.state.gateway is not None
    assert app.state.gateway.resolve() == "/dev/ttyUSB0"


def test_without_no_gateway_a_gateway_is_still_constructed_even_with_no_device(
    tmp_path: Path,
) -> None:
    """No `--serial-device` and no `--no-gateway` is a real deployment
    shape: hardware not plugged in yet, or resolved entirely by `by-id`/
    VID:PID at a later revision of this flag set. The gateway must still
    come up and simply report the link down (`adapters/serial_gateway.py`
    step 4), never crash the whole station over a missing radio.
    """
    app, args = build_app(["--db-path", str(tmp_path / "station.db")])

    assert args.no_gateway is False
    assert app.state.gateway is not None
    assert app.state.gateway.resolve() is None


def test_station_config_flags_share_the_same_argv(tmp_path: Path) -> None:
    """test_config.py's own precedent: a later slice's cli.py flags must
    share `argv` with `StationConfig.load` without either parser choking on
    the other's flags, and `build_app` must actually hand that parsed
    config to `create_app` -- not a fresh, flag-less default.
    """
    argv = [
        "--db-path",
        str(tmp_path / "station.db"),
        "--no-gateway",
        "--backlog-retry-budget",
        "5",
    ]

    app, _args = build_app(argv)

    expected_config = StationConfig.load(argv)
    assert app.state.config.backlog_retry_budget == 5
    assert app.state.config == expected_config
