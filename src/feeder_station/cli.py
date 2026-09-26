"""Starts the station.

Binds every interface on purpose: the operator reaches it from a phone on the
local network (S10), and there is no route in from outside the site anyway.

12.8: this is also where the gateway actually gets turned on (design.md
section 7: "the gateway is off by default and turned on by cli.py when a
device is configured") -- ``--db-path`` and ``--serial-device`` point it at
real on-disk state and a real device, and ``--no-gateway`` is the rollback
kill switch that starts the HTTP interface with no gateway or scheduler
thread at all, whatever hardware is or is not attached.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import uvicorn
from fastapi import FastAPI

from feeder_station.adapters.network_gateway import (
    DEFAULT_TCP_PORT,
    NetworkGateway,
    NetworkGatewayConfig,
)
from feeder_station.adapters.serial_gateway import DeviceConfig, SerialGateway
from feeder_station.adapters.store import Store
from feeder_station.app import create_app
from feeder_station.config import StationConfig

DEFAULT_DB_PATH = "station.db"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="feeder-station", add_help=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--db-path",
        default=DEFAULT_DB_PATH,
        help="Path to the station's own SQLite event store (S9).",
    )
    parser.add_argument(
        "--serial-device",
        default=None,
        help=(
            "Explicit serial device path -- step 1 of the 4-step device "
            "resolution order (adapters/serial_gateway.py::resolve_device). "
            "Omit it to fall back to by-id/VID:PID resolution."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("serial", "network"),
        default="serial",
        help=(
            "Which link the station listens for nodes on. 'serial' (the "
            "default, unchanged) is the bench's own USB cable; 'network' is "
            "a listening TCP socket nodes dial into over Wi-Fi -- a second "
            "option, not a replacement (this change's own report). "
            "--serial-device is ignored when this is 'network', and "
            "--network-host/--network-port are ignored when it is 'serial'."
        ),
    )
    parser.add_argument(
        "--network-host",
        default=NetworkGatewayConfig.host,
        help=(
            "Bind address for --transport network (default: every "
            "interface, the same LAN trust model this station's own HTTP "
            "server already binds -- adapters/network_gateway.py's own "
            "module docstring, and this change's report, name what that "
            "does and does not secure)."
        ),
    )
    parser.add_argument(
        "--network-port",
        type=int,
        default=DEFAULT_TCP_PORT,
        help="Listening port for --transport network.",
    )
    parser.add_argument(
        "--no-gateway",
        action="store_true",
        help=(
            "Rollback kill switch: start with no gateway or scheduler "
            "thread at all, regardless of what hardware is attached or "
            "which --transport was requested."
        ),
    )
    return parser


def build_app(argv: Sequence[str]) -> tuple[FastAPI, argparse.Namespace]:
    """Everything ``main()`` needs before handing off to ``uvicorn.run`` --
    pulled out so tests can construct the real app and inspect what each
    flag produced without starting a server.

    ``argv`` is shared, unfiltered, with :meth:`StationConfig.load`
    (``tests/test_config.py``'s own precedent: "a later slice's cli.py
    flags share this argv" — each parser's ``parse_known_args`` ignores
    what it does not recognise, so neither one has to know about the
    other's flags).
    """
    args, _unknown = _build_parser().parse_known_args(list(argv))
    config = StationConfig.load(argv)
    store = Store(args.db_path)

    gateway: SerialGateway | NetworkGateway | None = None
    if not args.no_gateway:
        if args.transport == "network":
            gateway = NetworkGateway(
                NetworkGatewayConfig(host=args.network_host, port=args.network_port)
            )
        else:
            # No device resolves (step 4, adapters/serial_gateway.py) is a
            # real, supported shape here too -- the radio not being plugged
            # in yet must never crash station startup; the gateway thread
            # just reports the link down and keeps retrying.
            gateway = SerialGateway(DeviceConfig(explicit_path=args.serial_device))

    app = create_app(config=config, store=store, gateway=gateway)
    return app, args


def main(argv: list[str] | None = None) -> None:
    resolved_argv = list(sys.argv[1:]) if argv is None else list(argv)
    app, args = build_app(resolved_argv)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
