"""Whether a newer station has been released on GitHub, and getting it.

Only the packaged executable asks (cli.py builds an :class:`UpdateChecker`
when frozen): a source checkout updates with git. The repository is public,
so the GitHub API needs no token.

What "getting it" means depends on how this copy runs:

- installed (Windows installer, or Linux ``--instalar``): one click downloads
  the new version and installs it. On Windows that is the new installer,
  opened while the station shuts itself down so its files are free; on
  Linux the new binary reinstalls itself over ``~/.local/bin``, and the
  operator reopens the station from the menu.
- portable: the notice links to the new portable file; replacing it is the
  operator's own step, which is the point of a portable copy.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = "JuanReyes01/feeder-field-station"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=20"
CHECK_EVERY_S = 24 * 3600.0
RETRY_AFTER_FAILURE_S = 3600.0
INSTALLED_MARKER = "instalado.txt"  # written next to the .exe by the Windows installer

INSTALLED = "instalada"
PORTABLE = "portable"

# Asset names, the same ones .github/workflows/release.yml publishes.
_ASSET_SUFFIX = {
    ("windows", INSTALLED): "-Windows-Instalador.exe",
    ("windows", PORTABLE): "-Windows-Portable.exe",
    # A Linux installed copy updates from the portable binary: that binary
    # reinstalls itself with --instalar.
    ("linux", INSTALLED): "-Linux-Portable",
    ("linux", PORTABLE): "-Linux-Portable",
}


def asset_name(version: str, system: str, mode: str) -> str:
    return f"EstacionCICG-{version}{_ASSET_SUFFIX[(system, mode)]}"


# --- versions ------------------------------------------------------------------

_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-.]?(a|b|rc)\.?(\d+))?$", re.IGNORECASE)
_STAGE = {"a": 0, "b": 1, "rc": 2}


def parse_version(text: str) -> tuple[tuple[int, ...], tuple[int, int]] | None:
    """``0.2.0`` / ``v0.2.0-rc2`` / ``0.2.0rc2`` -> a sortable key; a final
    release sorts after every pre-release of the same numbers. None when
    the text is not a version at all."""
    match = _VERSION.match(text.strip())
    if match is None:
        return None
    numbers = tuple(int(part) for part in match.group(1).split("."))
    numbers = numbers + (0,) * (3 - len(numbers)) if len(numbers) < 3 else numbers
    if match.group(2) is None:
        return numbers, (9, 0)
    return numbers, (_STAGE[match.group(2).lower()], int(match.group(3)))


def is_prerelease(version: str) -> bool:
    parsed = parse_version(version)
    return parsed is not None and parsed[1][0] != 9


@dataclass(frozen=True)
class Release:
    version: str
    html_url: str
    # asset name -> (download URL, sha256 hex or None)
    assets: Mapping[str, tuple[str, str | None]]


def pick_release(releases: Sequence[Mapping[str, Any]], current: str) -> Release | None:
    """The newest published release newer than ``current``. Pre-releases
    are offered only to a copy that is itself a pre-release, so the field
    never gets a test build unasked."""
    current_key = parse_version(current)
    if current_key is None:
        return None
    allow_pre = is_prerelease(current)
    best: tuple[Any, Release] | None = None
    for item in releases:
        if item.get("draft") or (item.get("prerelease") and not allow_pre):
            continue
        tag = str(item.get("tag_name", ""))
        key = parse_version(tag)
        if key is None or key <= current_key:
            continue
        assets: dict[str, tuple[str, str | None]] = {}
        for asset in item.get("assets") or []:
            digest = str(asset.get("digest") or "")
            sha256 = digest.removeprefix("sha256:") if digest.startswith("sha256:") else None
            assets[str(asset.get("name"))] = (str(asset.get("browser_download_url")), sha256)
        release = Release(
            version=tag.removeprefix("v"), html_url=str(item.get("html_url")), assets=assets
        )
        if best is None or key > best[0]:
            best = (key, release)
    return None if best is None else best[1]


# --- how this copy runs ----------------------------------------------------------


def current_system() -> str:
    return "windows" if sys.platform == "win32" else "linux"


def install_mode(executable: Path, system: str, home: Path) -> str:
    """Installed when the Windows installer left its marker next to the
    .exe, or when this is the copy Linux ``--instalar`` put in
    ``~/.local/bin``; portable otherwise."""
    if system == "windows":
        return INSTALLED if (executable.parent / INSTALLED_MARKER).exists() else PORTABLE
    installed = home / ".local" / "bin" / "cicg-estacion"
    try:
        return INSTALLED if executable.resolve() == installed.resolve() else PORTABLE
    except OSError:
        return PORTABLE


# --- network -----------------------------------------------------------------------

_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",  # Debian, Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # Fedora, RHEL
    "/etc/ssl/ca-bundle.pem",  # openSUSE
    "/etc/ssl/cert.pem",
)


def _ssl_context() -> ssl.SSLContext:
    """The bundled OpenSSL of a frozen Linux binary looks for certificates
    where the build machine kept them, which is not where every
    distribution does; point it at whichever system bundle exists."""
    context = ssl.create_default_context()
    if sys.platform != "win32":
        for bundle in _CA_BUNDLES:
            if os.path.exists(bundle):
                context.load_verify_locations(cafile=bundle)
                break
    return context


def _request(url: str, user_agent: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "*/*"})


def fetch_releases(user_agent: str) -> list[dict[str, Any]]:
    request = _request(RELEASES_URL, user_agent)
    request.add_header("Accept", "application/vnd.github+json")
    with urllib.request.urlopen(request, timeout=15, context=_ssl_context()) as response:
        data = json.load(response)
    return data if isinstance(data, list) else []


def download(url: str, target: Path, user_agent: str) -> None:
    with (
        urllib.request.urlopen(
            _request(url, user_agent), timeout=60, context=_ssl_context()
        ) as response,
        open(target, "wb") as handle,
    ):
        while chunk := response.read(1 << 16):
            handle.write(chunk)


# --- the checker the app talks to ----------------------------------------------------


class UpdateChecker:
    """Checks GitHub at most once a day, in the background, and runs the
    one-click update for an installed copy. Every outside effect is
    injected, so tests drive it without a network or a second process."""

    def __init__(
        self,
        *,
        current: str,
        system: str,
        mode: str,
        fetch: Callable[[], list[dict[str, Any]]],
        download: Callable[[str, Path], None],
        launch_installer: Callable[[Path], None],
        reinstall: Callable[[Path], None],
        request_shutdown: Callable[[], None],
        monotonic: Callable[[], float] = time.monotonic,
        background: bool = True,
    ) -> None:
        self.current = current
        self.system = system
        self.mode = mode
        self._fetch = fetch
        self._download = download
        self._launch_installer = launch_installer
        self._reinstall = reinstall
        self._request_shutdown = request_shutdown
        self._monotonic = monotonic
        self._background = background
        self._lock = threading.Lock()
        self._release: Release | None = None
        self._next_check = 0.0
        self._checking = False
        self._state = "idle"  # idle | downloading | installing | done | error
        self._message: str | None = None

    def status(self) -> dict[str, Any]:
        self._check_if_due()
        with self._lock:
            release = self._release
            name = asset_name(release.version, self.system, self.mode) if release else None
            asset = release.assets.get(name) if release and name else None
            return {
                "enabled": True,
                "current": self.current,
                "mode": self.mode,
                "available": asset is not None,
                "latest": release.version if release else None,
                "notes_url": release.html_url if release else None,
                "url": asset[0] if asset else None,
                "action": "install" if self.mode == INSTALLED else "download",
                "state": self._state,
                "message": self._message,
            }

    def check_now(self) -> dict[str, Any]:
        """The "Buscar actualizaciones" button: ask GitHub now, not tomorrow."""
        with self._lock:
            self._next_check = 0.0
        self._check_if_due()
        return self.status()

    def _check_if_due(self) -> None:
        with self._lock:
            if self._checking or self._monotonic() < self._next_check:
                return
            self._checking = True
        if self._background:
            threading.Thread(target=self._check, name="update-check", daemon=True).start()
        else:
            self._check()

    def _check(self) -> None:
        try:
            release = pick_release(self._fetch(), self.current)
            delay = CHECK_EVERY_S
        except (OSError, ValueError):  # offline, rate-limited, GitHub down: try later, say nothing
            release = None
            delay = RETRY_AFTER_FAILURE_S
        with self._lock:
            if release is not None or delay == CHECK_EVERY_S:
                self._release = release
            self._next_check = self._monotonic() + delay
            self._checking = False

    def start_update(self) -> dict[str, Any]:
        """The "Actualizar" button: an installed copy only, once at a time."""
        with self._lock:
            if self.mode != INSTALLED:
                raise ValueError("La versión portable se actualiza descargando el archivo nuevo.")
            release = self._release
            if release is None:
                raise ValueError("No hay una versión nueva.")
            name = asset_name(release.version, self.system, self.mode)
            if name not in release.assets:
                raise ValueError(f"El release {release.version} no trae {name}.")
            if self._state in ("downloading", "installing"):
                raise ValueError("La actualización ya está en curso.")
            self._state = "downloading"
            self._message = f"Descargando la versión {release.version}…"
            url, sha256 = release.assets[name]
        args = (release.version, name, url, sha256)
        if self._background:
            threading.Thread(target=self._update, args=args, name="update", daemon=True).start()
        else:
            self._update(*args)
        return self.status()

    def _update(self, version: str, name: str, url: str, sha256: str | None) -> None:
        try:
            folder = Path(tempfile.mkdtemp(prefix="cicg-estacion-"))
            target = folder / name
            self._download(url, target)
            if sha256 is not None:
                actual = hashlib.sha256(target.read_bytes()).hexdigest()
                if actual != sha256:
                    raise RuntimeError("el archivo descargado no coincide con el del release")
            self._set("installing", f"Instalando la versión {version}…")
            if self.system == "windows":
                self._launch_installer(target)
                self._set(
                    "done", "Se abrió el instalador; la estación se cierra para dejarlo trabajar."
                )
                self._request_shutdown()
            else:
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                self._reinstall(target)
                self._set(
                    "done",
                    f"Versión {version} instalada: cierra esta ventana y vuelve a abrir Estación CICG.",
                )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            self._set("error", f"No se pudo actualizar: {exc}")

    def _set(self, state: str, message: str) -> None:
        with self._lock:
            self._state = state
            self._message = message


def launch_installer(installer: Path) -> None:
    """Starts the Windows installer as its own process, outliving this one."""
    subprocess.Popen([str(installer)], close_fds=True)


def reinstall_from(binary: Path) -> None:
    """Runs the new Linux binary's own ``--instalar``, which replaces the
    installed copy (rename over the running file, never a write into it)."""
    subprocess.run([str(binary), "--instalar"], check=True, capture_output=True, timeout=120)
