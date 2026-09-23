"""The station's own event export: filtered CSV, `VACUUM INTO` snapshot,
one documented folder. Every scenario here traces to
`openspec/changes/field-station-p4/specs/data-export/spec.md`.

The one test that matters most is
``test_export_under_concurrent_writes_omits_nothing_acknowledged``: it is
the mechanical proof of D6 -- that a `VACUUM INTO` snapshot, unlike a plain
`cp` of a live WAL database, never drops a row this station already told a
trap was safe.
"""

from __future__ import annotations

import csv
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from feeder_station.adapters import export
from feeder_station.adapters.store import Store, open_connection
from feeder_station.domain import payloads
from feeder_station.domain.events import (
    OPERATOR_ARM,
    OPERATOR_CAPTURE_ACKNOWLEDGED,
    OPERATOR_DISARM,
    OPERATOR_SET_TARGETS,
    STATION_TIME_SYNC_WITHHELD,
    Source,
    StationEvent,
)

TAG = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])


def _event(
    *,
    source: Source = "node",
    node_id: int = 1,
    msg_type: int,
    msg_id: int | None = None,
    tag: bytes | None = None,
) -> StationEvent:
    return StationEvent(
        source=source,
        node_id=node_id,
        msg_type=msg_type,
        msg_id=msg_id,
        node_epoch=None,
        tag=tag,
        fields={},
        payload=b"",
    )


# --- 11.1/11.2: only D1's witnessed types reach the CSV ----------------------


def test_export_contains_only_station_witnessed_event_types(tmp_path: Path) -> None:
    """D1: visits, captures, and the trap-state changes the station can
    actually observe (a node's own FAULT report, an operator's arm/disarm)
    -- and nothing else. `HEARTBEAT` is a continuous *report* of current
    state, not a *change*, and is deliberately excluded (see
    `adapters/export.py`'s module docstring); `HELLO`/`ACK` are protocol
    handshake, not witnessed facts; `OPERATOR_SET_TARGETS`,
    `OPERATOR_CAPTURE_ACKNOWLEDGED` and `STATION_TIME_SYNC_WITHHELD` are
    station bookkeeping, not a visit, a capture, or a trap-state change.
    """
    store = Store(tmp_path / "station.db")

    # Exportable.
    store.append(
        _event(msg_type=payloads.VISIT, msg_id=1, tag=TAG), received_epoch=1.0, clock_trust="unset"
    )
    store.append(
        _event(msg_type=payloads.CAPTURED, msg_id=2, tag=TAG),
        received_epoch=2.0,
        clock_trust="unset",
    )
    store.append(_event(msg_type=payloads.FAULT, msg_id=3), received_epoch=3.0, clock_trust="unset")
    store.append(
        _event(source="operator", msg_type=OPERATOR_ARM), received_epoch=4.0, clock_trust="unset"
    )
    store.append(
        _event(source="operator", msg_type=OPERATOR_DISARM),
        received_epoch=5.0,
        clock_trust="unset",
    )

    # Not exportable.
    store.append(
        _event(msg_type=payloads.HEARTBEAT, msg_id=6), received_epoch=6.0, clock_trust="unset"
    )
    store.append(_event(msg_type=payloads.HELLO, msg_id=7), received_epoch=7.0, clock_trust="unset")
    store.append(_event(msg_type=payloads.ACK, msg_id=8), received_epoch=8.0, clock_trust="unset")
    store.append(
        _event(source="operator", msg_type=OPERATOR_SET_TARGETS),
        received_epoch=9.0,
        clock_trust="unset",
    )
    store.append(
        _event(source="operator", msg_type=OPERATOR_CAPTURE_ACKNOWLEDGED),
        received_epoch=10.0,
        clock_trust="unset",
    )
    store.append(
        _event(source="station", msg_type=STATION_TIME_SYNC_WITHHELD),
        received_epoch=11.0,
        clock_trust="unset",
    )

    csv_path = tmp_path / "events.csv"
    export.export_events_csv(store._conn, csv_path)

    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    msg_types = {int(row["msg_type"]) for row in rows}
    assert msg_types == {
        payloads.VISIT,
        payloads.CAPTURED,
        payloads.FAULT,
        OPERATOR_ARM,
        OPERATOR_DISARM,
    }
    assert len(rows) == 5
    assert all(row["source"] in ("node", "operator") for row in rows)


# --- 11.3/11.4: labelled as the station's own record --------------------------


def test_export_is_labelled_as_the_stations_own_record(tmp_path: Path) -> None:
    """The provenance lives in the folder name and the README (DD13), not a
    `#` comment in the CSV that would break a naive CSV reader.
    """
    store = Store(tmp_path / "station.db")
    store.append(
        _event(msg_type=payloads.VISIT, msg_id=1, tag=TAG), received_epoch=1.0, clock_trust="unset"
    )

    folder = export.export_station(store, tmp_path / "media")

    assert folder.name.startswith("station-export-")

    readme = (folder / "README.txt").read_text(encoding="utf-8")
    assert "station's own record" in readme
    assert "node's SD card" in readme

    with (folder / "events.csv").open(encoding="utf-8") as fh:
        first_line = fh.readline()
    assert not first_line.startswith("#")
    assert first_line.startswith("seq,")


# --- 11.5/11.6: consistent snapshot under concurrent writes (D6) -------------


def test_export_under_concurrent_writes_omits_nothing_acknowledged(tmp_path: Path) -> None:
    """D6, the whole point of this slice.

    A writer thread -- its own connection, per design.md section 7's "one
    per thread" rule -- keeps appending and durably committing events while
    the main thread takes a `VACUUM INTO` snapshot. `wal_autocheckpoint`
    never fires for this small a workload, so every one of those commits
    lives *only* in `station.db-wal` at the moment the snapshot is taken --
    exactly the rows a plain `cp station.db` would silently miss. Every
    event the writer saw `append()` return for (durably committed, S9)
    before the snapshot started must be in the snapshot afterwards.
    """
    db_path = tmp_path / "station.db"
    store = Store(db_path)

    acknowledged: list[int] = []
    stop = threading.Event()

    def writer() -> None:
        writer_store = Store(db_path)
        try:
            msg_id = 0
            while not stop.is_set():
                event = _event(msg_type=payloads.VISIT, msg_id=msg_id, tag=TAG)
                writer_store.append(event, received_epoch=float(msg_id), clock_trust="unset")
                acknowledged.append(msg_id)  # only after append() returns: durable (S9)
                msg_id += 1
        finally:
            writer_store.close()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while len(acknowledged) < 20 and time.monotonic() < deadline:
            pass
        assert len(acknowledged) >= 20, "writer thread made no progress"

        before_export = list(acknowledged)
        snapshot_path = tmp_path / "export" / "station.db"
        snapshot_path.parent.mkdir()
        store.snapshot_into(snapshot_path)
    finally:
        stop.set()
        thread.join()

    # Confirm this test actually exercised D6's scenario: the live file
    # never grew past its header page, so every committed row really was
    # sitting only in `-wal`, uncheckpointed, when the snapshot was taken.
    assert db_path.stat().st_size <= 4096

    snapshot_conn = open_connection(snapshot_path)
    try:
        exported_msg_ids = {
            row[0]
            for row in snapshot_conn.execute(
                "SELECT msg_id FROM event WHERE node_id = 1"
            ).fetchall()
        }
    finally:
        snapshot_conn.close()

    missing = set(before_export) - exported_msg_ids
    assert not missing, f"snapshot dropped acknowledged events: {sorted(missing)}"


# --- 11.7/11.8: never a plain file copy ---------------------------------------


def test_plain_file_copy_is_not_the_export_path(tmp_path: Path) -> None:
    """A mechanical guard, not the D6 proof itself (that is the concurrent
    -write test above). This one pins the *mechanism*: the export must
    never fall back to `shutil.copy`/`copyfile`/`copy2` of the live file --
    only `Store.snapshot_into`'s `VACUUM INTO` (DD13) may produce the
    database copy.
    """
    store = Store(tmp_path / "station.db")
    store.append(
        _event(msg_type=payloads.VISIT, msg_id=1, tag=TAG), received_epoch=1.0, clock_trust="unset"
    )

    with (
        patch("shutil.copy") as copy,
        patch("shutil.copyfile") as copyfile,
        patch("shutil.copy2") as copy2,
    ):
        export.export_station(store, tmp_path / "media")

    copy.assert_not_called()
    copyfile.assert_not_called()
    copy2.assert_not_called()


# --- 11.9/11.10: one documented step produces a complete, openable copy ------


def test_documented_copy_step_produces_a_complete_openable_copy(tmp_path: Path) -> None:
    """CI proves the copy mechanism is mechanically correct and complete:
    the assembled folder opens, `PRAGMA integrity_check` passes, and every
    stored row -- not just the CSV's filtered view -- is present in
    `station.db`.

    It does **not** prove S14's usability half: that a biologist with no
    training can follow the written runbook step unaided. That is a field
    gate, named as such in `specs/data-export/spec.md`'s own scenario note,
    and no test -- here or anywhere -- can close it.
    """
    store = Store(tmp_path / "station.db")
    store.append(
        _event(msg_type=payloads.VISIT, msg_id=1, tag=TAG), received_epoch=1.0, clock_trust="unset"
    )
    store.append(
        _event(msg_type=payloads.CAPTURED, msg_id=2, tag=TAG),
        received_epoch=2.0,
        clock_trust="unset",
    )
    store.append(
        _event(msg_type=payloads.HEARTBEAT, msg_id=3), received_epoch=3.0, clock_trust="unset"
    )

    folder = export.export_station(
        store, tmp_path / "media", now=datetime(2026, 9, 23, 14, 12, 5, tzinfo=UTC)
    )

    assert folder == tmp_path / "media" / "station-export-20260923T141205Z"
    assert folder.is_dir()

    db_path = folder / "station.db"
    assert db_path.exists()
    snapshot_conn = open_connection(db_path)
    try:
        assert snapshot_conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        total_rows = snapshot_conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    finally:
        snapshot_conn.close()
    # The database copy is the *complete* record (S14), unfiltered -- all 3
    # rows, including the HEARTBEAT the CSV leaves out.
    assert total_rows == 3

    with (folder / "events.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2  # D1's filtered, witnessed-events view

    assert (folder / "README.txt").exists()
