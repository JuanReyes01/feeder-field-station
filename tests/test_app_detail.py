"""One bird's page and the Visits summary: the record of every capture, how
its measures vary, what the traps saw, and the period's totals."""

import io
import sqlite3
from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from feeder_station.adapters.store import SCHEMA_VERSION, Store
from feeder_station.app import create_app
from feeder_station.domain import payloads
from feeder_station.domain.events import StationEvent
from feeder_station.domain.measures import summarize
from feeder_station.domain.tags import parse_tag

OLD_PIT = "900_263002593695"
NEW_PIT = "900_263007596629"
OLD = parse_tag(OLD_PIT)
NEW = parse_tag(NEW_PIT)
MISREAD = bytes.fromhex("0384000000BEEF")


def _day(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day)  # noqa: DTZ001 - an Excel date cell has no zone


def _database() -> bytes:
    """One bird that lost its first tag, with a recapture written without the PIT."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Birds 2025"
    sheet.append(["CICG CODE", None, "MEASURES"])
    sheet.append(
        [
            "CICG_ID", "Especie", "Pit tag code", "Project", "Localidad", "Date",
            "Other code", "Time captured", "Tagged by", "Measured by", "weight", "wing length",
        ]
    )  # fmt: skip
    sheet.append(
        ["CICG_1", "Colibri coruscans", OLD_PIT, "CICG", "CICG", _day(2025, 1, 1),
         None, time(9, 5), "MAMA", "MAMA", 7.0, 68]
    )  # fmt: skip
    sheet.append(
        ["CICG_1", "Colibri coruscans", NEW_PIT, "CICG", "CICG", _day(2025, 6, 1),
         OLD_PIT, time(10, 30), "JJF", "JJF", 7.4, 69]
    )  # fmt: skip
    sheet.append(
        ["CICG_1", "Colibri coruscans", None, "CICG", "Zona alta", _day(2026, 2, 10),
         None, None, None, "AMF", 9.0, 69]
    )  # fmt: skip
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _client(tmp_path: Path) -> tuple[TestClient, Store]:
    store = Store(tmp_path / "station.db")
    client = TestClient(create_app(store=store))
    response = client.post("/api/specimens/import?filename=base.xlsx", content=_database())
    assert response.status_code == 200
    return client, store


def _read(store: Store, tag: bytes, epoch: float, node_id: int = 1, seq: int = 1) -> None:
    store.append(
        StationEvent(
            source="node",
            node_id=node_id,
            msg_type=payloads.READS,
            msg_id=seq,
            node_epoch=int(epoch),
            tag=tag,
            fields={"tag": tag.hex(), "epoch": int(epoch), "clock_trusted": True},
            payload=b"",
        ),
        received_epoch=epoch,
        clock_trust="ntp",
    )


def test_a_birds_page_joins_every_tag_it_wore(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    assert OLD is not None and NEW is not None
    start = datetime(2026, 3, 1, 7, tzinfo=UTC).timestamp()
    _read(store, NEW, start, node_id=1, seq=1)
    _read(store, NEW, start + 1, node_id=1, seq=2)  # the same visit
    _read(store, NEW, start + 3600, node_id=2, seq=3)
    page = client.get(f"/api/specimens/{NEW.hex().upper()}/detail").json()

    assert [(t["biomark"], t["replaced"]) for t in page["tags"]] == [
        (OLD_PIT, True),
        (NEW_PIT, False),
    ]
    assert [(c["day"], c["biomark"]) for c in page["captures"]] == [
        ("2025-01-01", OLD_PIT),
        ("2025-06-01", NEW_PIT),
        ("2026-02-10", NEW_PIT),  # written with the CICG code only
    ]
    assert page["recaptures"] == 2
    assert page["marked"]["day"] == "2025-01-01"
    assert page["marked"]["details"]["tagged_by"] == "MAMA"
    assert page["marked"]["details"]["time_captured"] == "09:05"
    assert page["last_capture"]["day"] == "2026-02-10"
    assert page["last_capture"]["locality"] == "Zona alta"

    weight = next(m for m in page["measures"] if m["key"] == "weight")
    assert (weight["count"], weight["first"], weight["last"]) == (3, 7.0, 9.0)
    assert (weight["min"], weight["max"], weight["median"]) == (7.0, 9.0, 7.4)
    assert weight["change"] == pytest.approx(2.0)
    assert weight["values"][-1]["deviation"] == pytest.approx((9.0 - 7.4) / 7.4)
    assert weight["unit"] == "g"
    assert {m["key"] for m in page["measures"]} == {"weight", "wing"}

    visits = page["visits"]
    assert (visits["count"], visits["reads"]) == (2, 3)
    assert (visits["first_epoch"], visits["first_node_id"]) == (start, 1)
    assert (visits["last_epoch"], visits["last_node_id"]) == (start + 3600, 2)
    assert sum(visits["by_hour"]) == 2
    assert {f["key"] for f in page["fields"]} >= {"weight", "tagged_by", "photo_wing"}

    # The lost tag opens the same bird.
    same = client.get(f"/api/specimens/{OLD.hex().upper()}/detail").json()
    assert same["captures"] == page["captures"]


def test_an_unknown_tag_has_no_page(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.get(f"/api/specimens/{MISREAD.hex().upper()}/detail").status_code == 404


def test_the_short_list_does_not_erase_a_captures_record(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert NEW is not None
    client.post(
        "/api/specimens/import?filename=lista.csv",
        content=f"PIT,Species,DateOfPIT\n{NEW_PIT},Colibri coruscans,Jun-1-2025\n".encode(),
    )
    page = client.get(f"/api/specimens/{NEW.hex().upper()}/detail").json()
    june = next(c for c in page["captures"] if c["day"] == "2025-06-01")
    assert june["details"]["weight"] == 7.4


def test_a_version_7_database_gains_the_capture_record(tmp_path: Path) -> None:
    path = tmp_path / "station.db"
    Store(path).close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE specimen_capture_day DROP COLUMN details")
    conn.execute("UPDATE meta SET value = '7' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    store = Store(path)
    client = TestClient(create_app(store=store))
    client.post("/api/specimens/import?filename=base.xlsx", content=_database())
    assert NEW is not None
    [record, *_] = store.capture_records([NEW])
    assert record.details["weight"] == 7.4
    store.close()
    conn = sqlite3.connect(path)
    version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    conn.close()
    assert version == str(SCHEMA_VERSION) == "8"


def test_the_visits_summary_covers_the_whole_period(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    assert NEW is not None
    now = datetime.now(UTC).timestamp()
    seq = 0
    for minutes in range(0, 50, 10):  # five visits of the bird, one trap
        seq += 1
        _read(store, NEW, now - 3600 - minutes * 60, node_id=1, seq=seq)
    seq += 1
    _read(store, MISREAD, now - 600, node_id=2, seq=seq)
    summary = client.get("/api/visits/summary?days=7").json()
    assert summary["visits"] == 6
    assert (summary["subjects"], summary["unknown_tags"], summary["unknown_visits"]) == (1, 1, 1)
    assert summary["reads"] == 6
    assert sum(summary["by_hour"]) == 6
    assert sum(d["visits"] for d in summary["by_day"]) == 6
    assert len(summary["by_day"]) == 7
    assert summary["top"][0]["biomark"] == NEW_PIT
    assert summary["top"][0]["cicg_id"] == "CICG_1"
    assert summary["top"][0]["visits"] == 5
    assert {s["label"]: s["visits"] for s in summary["by_species"]} == {
        "Colibri coruscans": 5,
        "Lectura desconocida": 1,
    }
    assert {n["node_id"]: n["visits"] for n in summary["by_node"]} == {1: 5, 2: 1}
    one_trap = client.get("/api/visits/summary?days=7&node_id=2").json()
    assert one_trap["visits"] == 1


def test_an_empty_period_has_an_empty_summary(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    summary = client.get("/api/visits/summary?days=1").json()
    assert (summary["visits"], summary["by_day"], summary["median_duration_s"]) == (0, [], None)


def test_a_measure_is_told_against_the_birds_own_median() -> None:
    summary = summarize([("2025-01-01", 7.0), ("2025-06-01", 7.4), ("2026-02-10", 9.0)])
    assert summary is not None
    assert summary.median == 7.4
    assert summary.cv == pytest.approx(summary.stdev / summary.mean)  # type: ignore[operator]
    assert summary.values[0].deviation == pytest.approx((7.0 - 7.4) / 7.4)
    single = summarize([("2025-01-01", 68.0)])
    assert single is not None
    assert (single.stdev, single.cv, single.change) == (None, None, 0.0)
    assert summarize([]) is None


def test_the_page_draws_its_summaries_without_loading_anything() -> None:
    from feeder_station import app as app_module

    html = (Path(app_module.__file__).parent / "static" / "index.html").read_text("utf-8")
    assert "/api/visits/summary" in html
    assert "/detail`" in html
    assert "<svg" in html and "://" not in html  # charts drawn inline, nothing fetched
