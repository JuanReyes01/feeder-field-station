"""packaging/release_notes.py: the release text names the four files the
workflow uploads, and the CHANGELOG section of that version."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "release_notes", ROOT / "packaging" / "release_notes.py"
)
assert spec is not None and spec.loader is not None
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)

CHANGELOG = """# Cambios

## 0.3.0

- Algo nuevo.

## 0.2.0

- Lo de antes.
"""


def test_the_notes_link_all_four_files() -> None:
    text = release_notes.notes("0.3.0", CHANGELOG)
    for suffix in (
        "Windows-Instalador.exe",
        "Windows-Portable.exe",
        "Linux-Instalador.tar.gz",
        "Linux-Portable",
    ):
        assert f"EstacionCICG-0.3.0-{suffix}" in text
    assert "/releases/download/v0.3.0/" in text
    assert "- Algo nuevo." in text
    assert "Lo de antes" not in text


def test_a_test_build_uses_the_section_of_the_version_it_leads_to() -> None:
    text = release_notes.notes("0.3.0-rc1", CHANGELOG)
    assert "- Algo nuevo." in text
    assert "Versión de prueba" in text


def test_the_real_changelog_has_the_current_version() -> None:
    from feeder_station import __version__

    text = release_notes.notes(__version__, (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert "Sin notas para esta versión" not in text
