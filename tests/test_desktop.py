"""desktop.py: the Linux executable puts itself in the application menu and
takes itself out, per user, never touching the station's data."""

from __future__ import annotations

import os
from pathlib import Path

from feeder_station import desktop


def _fake_executable(tmp_path: Path) -> Path:
    exe = tmp_path / "Descargas" / "cicg-estacion-linux-x86_64"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"\x7fELF fake")
    return exe


def test_install_writes_the_binary_icon_and_launcher(tmp_path: Path) -> None:
    home = tmp_path / "home"
    binary, icon, launcher = desktop.install(_fake_executable(tmp_path), home)

    assert binary == home / ".local/bin/cicg-estacion"
    assert binary.read_bytes() == b"\x7fELF fake"
    assert os.access(binary, os.X_OK)
    assert icon.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    entry = launcher.read_text(encoding="utf-8")
    assert "Name=Estación CICG" in entry
    assert f'Exec="{binary}"' in entry
    assert f"Icon={icon}" in entry
    assert "Terminal=true" in entry


def test_installing_again_updates_in_place(tmp_path: Path) -> None:
    home = tmp_path / "home"
    exe = _fake_executable(tmp_path)
    first = desktop.install(exe, home)
    exe.write_bytes(b"\x7fELF newer")
    second = desktop.install(exe, home)

    assert first == second
    assert first[0].read_bytes() == b"\x7fELF newer"


def test_installing_from_the_installed_copy_does_not_copy_onto_itself(tmp_path: Path) -> None:
    home = tmp_path / "home"
    binary, _, _ = desktop.install(_fake_executable(tmp_path), home)
    assert desktop.install(binary, home)[0] == binary
    assert binary.read_bytes() == b"\x7fELF fake"


def test_uninstall_removes_only_what_install_wrote(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data = home / "CICG" / "estacion.db"
    data.parent.mkdir(parents=True)
    data.write_bytes(b"field data")
    written = desktop.install(_fake_executable(tmp_path), home)

    assert desktop.uninstall(home) == written
    assert not any(path.exists() for path in written)
    assert data.read_bytes() == b"field data"
    assert desktop.uninstall(home) == []


def test_reinstalling_replaces_the_binary_by_renaming_never_writing_into_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    binary, _, _ = desktop.install(_fake_executable(tmp_path), home)
    inode_before = binary.stat().st_ino
    newer = tmp_path / "newer"
    newer.write_bytes(b"\x7fELF newer")

    desktop.install(newer, home)

    assert binary.read_bytes() == b"\x7fELF newer"
    assert binary.stat().st_ino != inode_before  # a new file, the running one untouched
    assert not binary.with_name("cicg-estacion.nuevo").exists()
