"""adapters/updates.py: the executable's update notice, with GitHub and the
installer both faked."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from feeder_station.adapters import updates
from feeder_station.adapters.updates import (
    INSTALLED,
    PORTABLE,
    UpdateChecker,
    asset_name,
    install_mode,
    parse_version,
    pick_release,
)


def _release(
    tag: str, *, prerelease: bool = False, draft: bool = False, digest: str | None = None
) -> dict[str, Any]:
    version = tag.removeprefix("v")
    names = [asset_name(version, s, m) for s in ("windows", "linux") for m in (INSTALLED, PORTABLE)]
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "html_url": f"https://github.com/x/releases/tag/{tag}",
        "assets": [
            {"name": n, "browser_download_url": f"https://dl/{n}", "digest": digest}
            for n in dict.fromkeys(names)
        ],
    }


def test_versions_sort_with_pre_releases_before_their_final() -> None:
    order = ["0.1.0", "0.2.0-rc1", "0.2.0rc2", "v0.2.0", "0.2.1", "0.10.0"]
    keys = [parse_version(v) for v in order]
    assert keys == sorted(keys)  # type: ignore[type-var]
    assert parse_version("not a version") is None


def test_the_newest_newer_release_is_offered() -> None:
    releases = [_release("v0.2.1"), _release("v0.3.0"), _release("v0.1.0")]
    picked = pick_release(releases, "0.2.0")
    assert picked is not None
    assert picked.version == "0.3.0"


def test_nothing_is_offered_when_this_is_the_newest() -> None:
    assert pick_release([_release("v0.2.0"), _release("v0.1.0")], "0.2.0") is None


def test_a_field_copy_never_gets_a_test_build_or_a_draft() -> None:
    releases = [_release("v0.3.0-rc1", prerelease=True), _release("v0.4.0", draft=True)]
    assert pick_release(releases, "0.2.0") is None


def test_a_test_build_is_offered_the_next_test_build() -> None:
    picked = pick_release([_release("v0.2.0-rc3", prerelease=True)], "0.2.0-rc2")
    assert picked is not None
    assert picked.version == "0.2.0-rc3"


def test_installed_or_portable(tmp_path: Path) -> None:
    exe = tmp_path / "Estacion CICG" / "cicg-estacion.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    assert install_mode(exe, "windows", tmp_path) == PORTABLE
    (exe.parent / "instalado.txt").write_text("x")
    assert install_mode(exe, "windows", tmp_path) == INSTALLED

    home = tmp_path / "home"
    installed = home / ".local" / "bin" / "cicg-estacion"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"\x7fELF")
    assert install_mode(installed, "linux", home) == INSTALLED
    assert (
        install_mode(tmp_path / "Descargas" / "EstacionCICG-Linux-Portable", "linux", home)
        == PORTABLE
    )


class _Harness:
    def __init__(
        self, *, system: str, mode: str, releases: list[dict[str, Any]], payload: bytes = b"new"
    ) -> None:
        self.fetches = 0
        self.downloaded: list[str] = []
        self.launched: list[Path] = []
        self.reinstalled: list[Path] = []
        self.shutdowns = 0
        self.now = 1000.0
        self.payload = payload
        self.releases = releases

        def fetch() -> list[dict[str, Any]]:
            self.fetches += 1
            return self.releases

        def download(url: str, target: Path) -> None:
            self.downloaded.append(url)
            target.write_bytes(self.payload)

        def shutdown() -> None:
            self.shutdowns += 1

        self.checker = UpdateChecker(
            current="0.2.0",
            system=system,
            mode=mode,
            fetch=fetch,
            download=download,
            launch_installer=self.launched.append,
            reinstall=self.reinstalled.append,
            request_shutdown=shutdown,
            monotonic=lambda: self.now,
            background=False,
        )


def test_the_check_happens_once_a_day() -> None:
    h = _Harness(system="windows", mode=INSTALLED, releases=[_release("v0.3.0")])
    status = h.checker.status()
    assert status["available"] is True
    assert status["latest"] == "0.3.0"
    assert status["action"] == "install"
    h.checker.status()
    assert h.fetches == 1
    h.now += updates.CHECK_EVERY_S
    h.checker.status()
    assert h.fetches == 2


def test_offline_says_nothing_and_tries_again_later() -> None:
    h = _Harness(system="windows", mode=INSTALLED, releases=[])

    def offline() -> list[dict[str, Any]]:
        h.fetches += 1
        raise OSError("no network")

    h.checker._fetch = offline
    assert h.checker.status()["available"] is False
    h.now += updates.RETRY_AFTER_FAILURE_S
    h.checker.status()
    assert h.fetches == 2


def test_a_portable_copy_is_pointed_at_the_new_portable_file() -> None:
    h = _Harness(system="windows", mode=PORTABLE, releases=[_release("v0.3.0")])
    status = h.checker.status()
    assert status["action"] == "download"
    assert status["url"] == "https://dl/EstacionCICG-0.3.0-Windows-Portable.exe"
    with pytest.raises(ValueError):
        h.checker.start_update()


def test_installed_on_windows_runs_the_new_installer_and_steps_aside() -> None:
    h = _Harness(system="windows", mode=INSTALLED, releases=[_release("v0.3.0")])
    h.checker.status()
    status = h.checker.start_update()

    assert h.downloaded == ["https://dl/EstacionCICG-0.3.0-Windows-Instalador.exe"]
    assert [p.name for p in h.launched] == ["EstacionCICG-0.3.0-Windows-Instalador.exe"]
    assert h.shutdowns == 1
    assert status["state"] == "done"


def test_installed_on_linux_reinstalls_from_the_new_binary() -> None:
    h = _Harness(system="linux", mode=INSTALLED, releases=[_release("v0.3.0")])
    h.checker.status()
    status = h.checker.start_update()

    [binary] = h.reinstalled
    assert binary.name == "EstacionCICG-0.3.0-Linux-Portable"
    assert binary.stat().st_mode & 0o100
    assert h.shutdowns == 0
    assert "vuelve a abrir" in status["message"]


def test_a_download_that_does_not_match_the_release_is_not_installed() -> None:
    digest = "sha256:" + hashlib.sha256(b"the real one").hexdigest()
    h = _Harness(
        system="windows",
        mode=INSTALLED,
        releases=[_release("v0.3.0", digest=digest)],
        payload=b"tampered",
    )
    h.checker.status()
    status = h.checker.start_update()
    assert status["state"] == "error"
    assert h.launched == []


def test_a_matching_digest_installs() -> None:
    digest = "sha256:" + hashlib.sha256(b"new").hexdigest()
    h = _Harness(system="windows", mode=INSTALLED, releases=[_release("v0.3.0", digest=digest)])
    h.checker.status()
    assert h.checker.start_update()["state"] == "done"


def test_no_newer_release_means_nothing_to_install() -> None:
    h = _Harness(system="windows", mode=INSTALLED, releases=[_release("v0.2.0")])
    assert h.checker.status()["available"] is False
    with pytest.raises(ValueError):
        h.checker.start_update()
