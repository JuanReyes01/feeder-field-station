"""adapters/settings.py: the interface's choices survive a restart, and a
missing or broken settings file never stops the station."""

from __future__ import annotations

from pathlib import Path

from feeder_station.adapters.settings import (
    StationSettings,
    load_settings,
    save_settings,
    settings_path_for,
)


def test_settings_live_next_to_their_database() -> None:
    assert settings_path_for("/data/campo.db") == Path("/data/campo.settings.json")
    assert settings_path_for(":memory:") is None


def test_settings_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "estacion.settings.json"
    save_settings(path, StationSettings(serial_device="COM3"))
    assert load_settings(path) == StationSettings(serial_device="COM3")
    assert not (tmp_path / "estacion.settings.json.tmp").exists()


def test_no_file_is_the_defaults(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "missing.settings.json") == StationSettings()
    assert load_settings(None) == StationSettings()


def test_a_broken_file_is_the_defaults_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "broken.settings.json"
    for text in ("{not json", "[1, 2]", '{"serial_device": 3}', '{"serial_device": ""}'):
        path.write_text(text, encoding="utf-8")
        assert load_settings(path) == StationSettings()


def test_saving_without_a_path_is_a_no_op() -> None:
    save_settings(None, StationSettings(serial_device="COM3"))
