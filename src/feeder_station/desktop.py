"""Puts the station's executable in the Linux application menu, and takes it
out again (``cicg-estacion --instalar`` / ``--desinstalar``).

Per user, nothing needs root: the executable is copied to ``~/.local/bin``,
the icon to ``~/.local/share/icons`` and a launcher to
``~/.local/share/applications``, where GNOME, KDE and the rest look for
"Estación CICG". The launcher opens the station in a terminal on purpose:
that window shows the address and the state, and closing it stops the
station, the same as on Windows.

Windows gets the same from its installer (packaging/cicg-estacion.iss).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_ID = "cicg-estacion"
ICON = Path(__file__).parent / "static" / "icon.png"

_DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Version=1.0
Name=Estación CICG
GenericName=Estación de campo
Comment=Trampas, individuos y visitas del Centro de Investigación Colibrí Gorriazul
Exec="{exec}"
Icon={icon}
Terminal=true
Categories=Science;Biology;
Keywords=CICG;colibrí;colibri;trampa;RFID;estación;estacion;
StartupNotify=false
"""


def _paths(home: Path) -> tuple[Path, Path, Path]:
    return (
        home / ".local" / "bin" / APP_ID,
        home / ".local" / "share" / "icons" / "hicolor" / "256x256" / "apps" / f"{APP_ID}.png",
        home / ".local" / "share" / "applications" / f"{APP_ID}.desktop",
    )


def install(executable: Path, home: Path) -> list[Path]:
    """Installs ``executable`` for the user whose home is ``home``; returns
    what it wrote. Running it again, from a newer executable, updates it."""
    binary, icon, launcher = _paths(home)
    for path in (binary, icon, launcher):
        path.parent.mkdir(parents=True, exist_ok=True)
    if executable.resolve() != binary.resolve():
        # Copied beside it and renamed over it: the installed copy may be
        # the very process running right now (an update from the station
        # itself), and writing into a running executable fails on Linux;
        # a rename leaves the running one intact until it exits.
        staged = binary.with_name(binary.name + ".nuevo")
        shutil.copy2(executable, staged)
        staged.chmod(0o755)
        os.replace(staged, binary)
    binary.chmod(0o755)
    shutil.copyfile(ICON, icon)
    # An absolute icon path: found at once, with no icon-cache refresh and
    # no new login.
    launcher.write_text(_DESKTOP_ENTRY.format(exec=binary, icon=icon), encoding="utf-8")
    return [binary, icon, launcher]


def uninstall(home: Path) -> list[Path]:
    """Removes what :func:`install` wrote; returns what was there. The
    station's data (``~/CICG``) is never touched."""
    removed = []
    for path in _paths(home):
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed
