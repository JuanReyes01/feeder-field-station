"""Starts the station.

Binds every interface on purpose: the operator reaches it from a phone on the
local network (S10), and there is no route in from outside the site anyway.

12.8: this is also where the gateway actually gets turned on (design.md
section 7: "the gateway is off by default and turned on by cli.py when a
device is configured") -- ``--db-path`` and ``--serial-device`` point it at
real on-disk state and a real device, and ``--no-gateway`` is the rollback
kill switch that starts the HTTP interface with no gateway or scheduler
thread at all, whatever hardware is or is not attached.

The same entry point is frozen into the executable a biologist double-clicks
(packaging/, `pyinstaller`): there, with no flags at all, the station keeps
its database in a CICG folder in the user's home, finds the gateway on its
own (the one USB serial port there is), opens the browser on the interface,
and keeps the console window open if it fails to start, so the reason can be
read. ``--instalar`` puts it in the Linux application menu (desktop.py).

Which port the gateway is on: ``--serial-device`` when given, otherwise the
port last chosen in the interface (saved next to the database,
adapters/settings.py), otherwise ``auto``.
"""

from __future__ import annotations

import argparse
import signal
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from feeder_station import __version__, desktop
from feeder_station.adapters import updates
from feeder_station.adapters.serial_gateway import AUTO_DEVICE, SerialGateway, device_config_for
from feeder_station.adapters.settings import load_settings, settings_path_for
from feeder_station.adapters.store import Store
from feeder_station.app import create_app
from feeder_station.config import StationConfig

DEFAULT_DB_PATH = "station.db"
FROZEN_DB_PATH = Path("CICG") / "estacion.db"  # under the user's home


def is_frozen() -> bool:
    """Whether this is the packaged executable rather than a source run."""
    return bool(getattr(sys, "frozen", False))


def default_db_path() -> str:
    """The executable keeps the database in the user's own CICG folder, where
    it survives the executable being replaced by a newer one; a source
    checkout keeps the old relative default."""
    if is_frozen():
        return str(Path.home() / FROZEN_DB_PATH)
    return DEFAULT_DB_PATH


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="feeder-station", add_help=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--db-path",
        default=default_db_path(),
        help="Path to the station's own SQLite event store (S9).",
    )
    parser.add_argument(
        "--serial-device",
        default=None,
        help=(
            "Explicit serial device path (/dev/ttyUSB0, COM3) -- step 1 of the "
            "device resolution order (adapters/serial_gateway.py::"
            "resolve_device). Left out: the port last chosen in the interface, "
            f"else '{AUTO_DEVICE}', which takes the only USB serial port there "
            "is and reports the link down when there are several."
        ),
    )
    parser.add_argument(
        "--instalar",
        action="store_true",
        help="Linux executable: add 'Estación CICG' to the application menu, then exit.",
    )
    parser.add_argument(
        "--desinstalar",
        action="store_true",
        help="Linux executable: remove it from the application menu (the data stays), then exit.",
    )
    parser.add_argument(
        "--open-browser",
        action=argparse.BooleanOptionalAction,
        default=is_frozen(),
        help="Open the interface in the default browser once the server is up.",
    )
    parser.add_argument(
        "--transport",
        choices=("serial",),
        default="serial",
        help=(
            "Which link the station listens for nodes on. Only 'serial' "
            "(the bench's own USB cable, gateway ESP32 <-> station) exists "
            "now -- the trap already talks ESP-NOW to that gateway, never "
            "Wi-Fi. A TCP/Wi-Fi transport nodes could dial into directly "
            "was removed: associating with a Wi-Fi AP drags an ESP32's "
            "radio to the AP's channel (Espressif's 'home channel' rule), "
            "the same radio ESP-NOW needs, and the site's Starlink network "
            "gives this station no control over that channel or its "
            "stability. The flag is kept, with one choice, rather than "
            "dropped outright, so an operator who still types "
            "'--transport network' out of habit gets an explicit 'invalid "
            "choice' error instead of the flag silently vanishing into "
            "build_app's own parse_known_args."
        ),
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
    Path(args.db_path).parent.mkdir(parents=True, exist_ok=True)
    store = Store(args.db_path)
    settings_path = settings_path_for(args.db_path)
    serial_device = args.serial_device or load_settings(settings_path).serial_device

    gateway: SerialGateway | None = None
    if not args.no_gateway:
        # No device resolves (step 4, adapters/serial_gateway.py) is a
        # real, supported shape here too -- the radio not being plugged
        # in yet must never crash station startup; the gateway thread
        # just reports the link down and keeps retrying.
        gateway = SerialGateway(device_config_for(serial_device))

    app = create_app(
        config=config,
        store=store,
        gateway=gateway,
        settings_path=settings_path,
        updates=update_checker() if is_frozen() else None,
    )
    return app, args


def _shut_down_soon() -> None:
    """Stops the server the way Ctrl+C does, a moment after the response
    that asked for it has gone out."""
    threading.Timer(2.0, signal.raise_signal, args=(signal.SIGINT,)).start()


def update_checker() -> updates.UpdateChecker:
    """The executable's own update notice (adapters/updates.py)."""
    user_agent = f"cicg-estacion/{__version__}"
    system = updates.current_system()
    return updates.UpdateChecker(
        current=__version__,
        system=system,
        mode=updates.install_mode(Path(sys.executable), system, Path.home()),
        fetch=lambda: updates.fetch_releases(user_agent),
        download=lambda url, target: updates.download(url, target, user_agent),
        launch_installer=updates.launch_installer,
        reinstall=updates.reinstall_from,
        request_shutdown=_shut_down_soon,
    )


def _desktop_command(args: argparse.Namespace) -> int:
    """``--instalar`` / ``--desinstalar``; returns the exit status."""
    if sys.platform == "win32":
        print("En Windows la estación se instala con cicg-estacion-setup.exe.")
        return 2
    if not is_frozen():
        print(
            "--instalar es para el ejecutable; desde el código fuente usa 'uv run feeder-station'."
        )
        return 2
    if args.desinstalar:
        removed = desktop.uninstall(Path.home())
        print("Quitado del menú de aplicaciones:" if removed else "No estaba instalado.")
        for path in removed:
            print(f"  {path}")
        print(f"Los datos de la estación siguen en {Path.home() / FROZEN_DB_PATH.parent}.")
        return 0
    written = desktop.install(Path(sys.executable), Path.home())
    print("Listo: busca 'Estación CICG' en tus aplicaciones.")
    for path in written:
        print(f"  {path}")
    return 0


def _open_browser_when_listening(port: int, *, timeout_s: float = 30.0) -> None:
    """Waits for the server to accept connections, then opens the page --
    opening it before uvicorn is listening shows the browser's own error."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.3)
    else:
        return
    webbrowser.open(f"http://localhost:{port}/")


def main(argv: list[str] | None = None) -> None:
    resolved_argv = list(sys.argv[1:]) if argv is None else list(argv)
    early, _ = _build_parser().parse_known_args(resolved_argv)
    if early.instalar or early.desinstalar:
        raise SystemExit(_desktop_command(early))
    try:
        app, args = build_app(resolved_argv)
        print(f"Estación CICG {__version__}", flush=True)
        print(f"Base de datos: {Path(args.db_path).resolve()}", flush=True)
        print(
            f"Interfaz: http://localhost:{args.port}/ (cierra esta ventana para apagar la estación)",
            flush=True,
        )
        if args.open_browser:
            threading.Thread(
                target=_open_browser_when_listening, args=(args.port,), daemon=True
            ).start()
        uvicorn.run(app, host=args.host, port=args.port)
    except (Exception, SystemExit) as exc:
        if not is_frozen():
            raise
        # A double-clicked console closes the moment the process exits:
        # without this the reason (a port already in use, an unreadable
        # database) vanishes before anyone can read it.
        if not isinstance(exc, SystemExit):
            print(f"\nLa estación no pudo arrancar: {exc!r}")
        input("\nPresiona Enter para cerrar esta ventana...")
        raise


if __name__ == "__main__":
    main()
