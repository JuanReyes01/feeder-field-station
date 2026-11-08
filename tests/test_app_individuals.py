"""A bird is its CICG code: the tags it wore, lost or in use, are one individual
in counts, names, pages, marking and exports."""

import csv
import io
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from openpyxl import Workbook

from feeder_station.adapters.store import Store
from feeder_station.app import create_app
from tests.test_app_detail import NEW, NEW_PIT, OLD, OLD_PIT, _client, _read


def _today(offset_s: float = 0.0) -> float:
    """A moment earlier today, whatever the hour the suite runs at."""
    now = datetime.now(UTC).astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return max(start + 1, now.timestamp() - 300) + offset_s


def test_a_bird_read_with_two_tags_counts_once(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    assert OLD is not None and NEW is not None
    _read(store, OLD, _today(), seq=1)  # the lost tag, read anyway
    _read(store, NEW, _today(60), seq=2)
    summary = client.get("/api/visits/summary?days=1").json()
    assert (summary["visits"], summary["subjects"], summary["unknown_tags"]) == (2, 1, 0)
    [top] = summary["top"]
    assert (top["individual"], top["cicg_id"], top["visits"]) == ("cicg:cicg_1", "CICG_1", 2)
    assert top["biomark"] == NEW_PIT  # named by the tag read last
    assert client.get("/api/overview").json()["subjects_today"] == 1


def test_the_log_names_the_bird_and_a_lost_tag(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    assert OLD is not None and NEW is not None
    _read(store, OLD, _today(), seq=1)
    _read(store, NEW, _today(60), seq=2)
    texts = [e["text"] for e in client.get("/api/log?kind=reads").json()["events"]]
    assert texts == [
        f"Lectura de CICG_1 · Colibri coruscans ({NEW_PIT})",
        f"Lectura de tag perdido de CICG_1 ({OLD_PIT})",
    ]


def test_a_birds_page_opens_by_code_and_stands_on_the_tag_in_use(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert OLD is not None and NEW is not None
    by_code = client.get("/api/individuals/cicg_1/detail").json()
    assert by_code["specimen"]["tag"] == NEW.hex().upper()
    assert by_code["in_use"] == [NEW.hex().upper()]
    assert by_code["individual"] == "cicg:cicg_1"
    by_lost_tag = client.get(f"/api/individuals/{OLD.hex().upper()}/detail").json()
    assert by_lost_tag["specimen"]["tag"] == NEW.hex().upper()
    assert by_lost_tag["opened"] == OLD.hex().upper()
    by_biomark = client.get(f"/api/individuals/{OLD_PIT}/detail").json()
    assert by_biomark["specimen"]["tag"] == NEW.hex().upper()
    assert client.get("/api/individuals/CICG_404/detail").status_code == 404


def _two_tags_in_use() -> bytes:
    """CICG_9 caught with one tag, then another, then the first again."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["CICG_ID", "Especie", "Pit tag code", "Date"])
    sheet.append(["CICG_9", "X", OLD_PIT, datetime(2026, 2, 26)])  # noqa: DTZ001
    sheet.append(["CICG_9", "X", NEW_PIT, datetime(2026, 5, 19)])  # noqa: DTZ001
    sheet.append(["CICG_9", "X", OLD_PIT, datetime(2026, 10, 25)])  # noqa: DTZ001
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_a_code_with_two_tags_in_use_is_reported_and_marks_both(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")
    client = TestClient(create_app(store=store))
    imported = client.post("/api/specimens/import?filename=b.xlsx", content=_two_tags_in_use())
    [warning] = imported.json()["warnings"]
    assert "CICG_9 tiene 2 tags en uso" in warning
    result = client.post(
        "/api/specimens/interest/change",
        json={"action": "add", "node_id": None, "entries": ["cicg_9"]},
    ).json()
    assert (result["changed"], result["ambiguous"], result["missing"]) == (2, [], [])
    assert OLD is not None and NEW is not None
    assert set(store.wanted_tags_for(1)) == {OLD, NEW}


def test_exports_carry_the_cicg_code_and_whether_the_tag_is_worn(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    assert OLD is not None and NEW is not None
    _read(store, OLD, _today(), seq=1)
    _read(store, NEW, _today(60), seq=2)
    for url in ("/api/export/reads.csv", "/api/export/visits.csv"):
        text = client.get(url).content.decode("utf-8-sig")
        rows = {r["biomark"]: r for r in csv.DictReader(io.StringIO(text))}
        assert rows[OLD_PIT]["codigo_cicg"] == rows[NEW_PIT]["codigo_cicg"] == "CICG_1"
        assert (rows[OLD_PIT]["tag_en_uso"], rows[NEW_PIT]["tag_en_uso"]) == ("no", "si")


def test_the_station_page_knows_its_version_data_and_last_import(tmp_path: Path) -> None:
    from feeder_station import __version__

    client, _ = _client(tmp_path)
    info = client.get("/api/station").json()
    assert info["version"] == __version__
    assert info["db_path"].endswith("station.db")
    assert info["backup_folder"].endswith("respaldos")
    assert info["last_import"]["filename"] == "base.xlsx"
    assert (info["last_import"]["tags"], info["last_import"]["individuals"]) == (2, 1)
    assert info["updates"] is False
    assert client.post("/api/update/check").json() == {"enabled": False, "available": False}


def test_a_backup_lands_in_the_data_folder(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    folder = Path(client.post("/api/backup").json()["folder"])
    assert folder.parent == (tmp_path / "respaldos").resolve()
    assert any(folder.iterdir())


def test_a_birds_visits_cover_every_tag_it_wore(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    assert OLD is not None and NEW is not None
    _read(store, OLD, _today(), seq=1)
    _read(store, NEW, _today(60), seq=2)
    visits = client.get("/api/visits?days=1&individual=CICG_1").json()["visits"]
    assert {v["biomark"] for v in visits} == {OLD_PIT, NEW_PIT}
    assert {v["cicg_id"] for v in visits} == {"CICG_1"}
    assert [v["lost_tag"] for v in visits] == [False, True]  # newest first
