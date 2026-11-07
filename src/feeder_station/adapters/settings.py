"""Station settings an operator changes from the interface and that must
survive a restart. Today that is only which serial port the gateway is on.

Kept in a small JSON file next to the database (``campo.db`` ->
``campo.settings.json``), so a station's settings travel with its data and
two databases on one machine never share them. A missing or unreadable file
is the defaults, never an error: the station must start either way.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class StationSettings:
    # A device path (/dev/ttyUSB0, COM3) or "auto"; None: never chosen.
    serial_device: str | None = None


def settings_path_for(db_path: str) -> Path | None:
    """Where the settings for the database at ``db_path`` live, or None for
    a database that is not a plain file (SQLite's ``:memory:``)."""
    if db_path == ":memory:" or db_path.startswith("file:"):
        return None
    path = Path(db_path)
    return path.with_name(path.stem + ".settings.json")


def load_settings(path: Path | None) -> StationSettings:
    if path is None:
        return StationSettings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return StationSettings()
    if not isinstance(raw, dict):
        return StationSettings()
    device = raw.get("serial_device")
    return StationSettings(serial_device=device if isinstance(device, str) and device else None)


def save_settings(path: Path | None, settings: StationSettings) -> None:
    """Written to a temporary file and renamed over the old one, so a power
    cut mid-write leaves either the old settings or the new, never half."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(settings), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
