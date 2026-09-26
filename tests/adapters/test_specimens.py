"""The specimen roster: ``adapters/store.py``'s ``specimen`` table.

``docs/pending.md``, "The station has one kind of storage and needs two":
observations are immutable and already fit the append-only ``event`` log;
specimens are mutable reference data and do not fit at all -- a tag, a
species, perhaps a band number, and whether the study wants this animal.
This module tests that table on its own terms, separately from
``test_store.py``'s existing event-log scenarios.

Two rules from that same section shape every test here:

**The roster grows from the data.** The first appearance of an unknown tag
*is* a new specimen -- not yet of interest, but known. Nothing is
pre-registered by hand.

**Analysis shapes should be views, not tables.** ``times_seen``/``last_seen``
are never columns on the ``specimen`` row -- they are computed fresh from
``event`` on every read, via the ``specimen_activity`` view, so they cannot
disagree with their own source. A denormalised ``last_seen`` column is
exactly the defect this project has hit eight times.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from feeder_station.adapters.store import SCHEMA_VERSION, Store
from feeder_station.domain import payloads
from feeder_station.domain.events import StationEvent

TAG_A = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])
TAG_B = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x00, 0x11])
TAG_C = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77])


def _read_event(*, node_id: int, msg_id: int, tag: bytes, epoch: int = 1000) -> StationEvent:
    """A ``READS``-shaped event -- the same batching ``interpret_reads``
    (domain/events.py) produces, minus the frame decode, so
    ``Store.append_batch`` can be exercised directly.
    """
    return StationEvent(
        source="node",
        node_id=node_id,
        msg_type=payloads.READS,
        msg_id=msg_id,
        node_epoch=epoch,
        tag=tag,
        fields={"tag": tag.hex(), "epoch": epoch, "clock_trusted": True, "batch_count": 1},
        payload=b"",
    )


def _captured_event(*, node_id: int, msg_id: int, tag: bytes) -> StationEvent:
    return StationEvent(
        source="node",
        node_id=node_id,
        msg_type=payloads.CAPTURED,
        msg_id=msg_id,
        node_epoch=2000,
        tag=tag,
        fields={"state_after": 0},
        payload=b"",
    )


def _heartbeat_event(*, node_id: int, msg_id: int) -> StationEvent:
    """No tag at all -- must never grow the roster."""
    return StationEvent(
        source="node",
        node_id=node_id,
        msg_type=payloads.HEARTBEAT,
        msg_id=msg_id,
        node_epoch=3000,
        tag=None,
        fields={"state": 0, "battery_mv": 4000, "targets_version": 0},
        payload=b"",
    )


# --- The roster grows from the data -----------------------------------------


def test_a_captured_read_creates_a_specimen_row_not_yet_of_interest(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")

    store.append(
        _captured_event(node_id=1, msg_id=1, tag=TAG_A), received_epoch=100.0, clock_trust="synced"
    )

    specimens = store.specimens()
    assert len(specimens) == 1
    row = specimens[0]
    assert row.tag == TAG_A
    assert row.species is None
    assert row.band_number is None
    assert row.of_interest is False  # pending.md: "not yet of interest, but known"


def test_a_reads_batch_creates_one_specimen_per_distinct_tag(tmp_path: Path) -> None:
    """A READS frame can carry more than one animal past the antenna in the
    same flush window -- ``append_batch``'s own dedup runs once per frame
    (one ``msg_id``), never once per read, so this proves the roster still
    gets one row per *distinct tag*, not one per row or one for the whole
    frame.
    """
    store = Store(tmp_path / "station.db")

    store.append_batch(
        [
            _read_event(node_id=1, msg_id=5, tag=TAG_A, epoch=1000),
            _read_event(node_id=1, msg_id=5, tag=TAG_B, epoch=1001),
            _read_event(node_id=1, msg_id=5, tag=TAG_A, epoch=1002),  # TAG_A again, same batch
        ],
        received_epoch=200.0,
        clock_trust="synced",
    )

    tags = {row.tag for row in store.specimens()}
    assert tags == {TAG_A, TAG_B}


def test_the_same_tag_seen_at_two_different_nodes_is_one_specimen(tmp_path: Path) -> None:
    """A specimen belongs to the study, not to a feeder (pending.md: "of
    interest is a flag on the specimen, not a list per feeder") -- the
    roster is keyed on the tag alone, never on ``(node_id, tag)``.
    """
    store = Store(tmp_path / "station.db")

    store.append(
        _captured_event(node_id=1, msg_id=1, tag=TAG_A), received_epoch=100.0, clock_trust="synced"
    )
    store.append(
        _captured_event(node_id=2, msg_id=1, tag=TAG_A), received_epoch=150.0, clock_trust="synced"
    )

    assert len(store.specimens()) == 1


def test_a_tagless_event_never_grows_the_roster(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")

    store.append(
        _heartbeat_event(node_id=1, msg_id=1), received_epoch=100.0, clock_trust="synced"
    )

    assert store.specimens() == []


# --- Migration: proven against a database the previous schema already wrote -


def _write_pre_specimen_database(path: Path) -> None:
    """A ``station.db`` exactly as ``SCHEMA_VERSION`` 1 would have left it --
    ``meta``/``event``/``node_cursor`` only, no ``specimen`` table, holding
    real rows, the same shape live bench data is in today. Written with raw
    ``sqlite3``, deliberately never through ``Store``, so this test does not
    depend on the very code path it exists to prove.
    """
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE event (
            seq            INTEGER PRIMARY KEY AUTOINCREMENT,
            node_seq       INTEGER,
            source         TEXT    NOT NULL,
            node_id        INTEGER NOT NULL,
            msg_type       INTEGER NOT NULL,
            msg_id         INTEGER,
            received_epoch REAL    NOT NULL,
            clock_trust    TEXT    NOT NULL,
            node_epoch     INTEGER,
            tag            BLOB,
            fields         TEXT    NOT NULL,
            payload        BLOB
        );
        CREATE TABLE node_cursor (
            node_id       INTEGER PRIMARY KEY,
            last_msg_id   INTEGER NOT NULL,
            last_seq      INTEGER NOT NULL,
            last_node_seq INTEGER NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
    # Two historic reads, from before the roster table existed -- exactly
    # what a bench database already holds. One HEARTBEAT (no tag) proves the
    # backfill does not fabricate a specimen for a row that never had one.
    conn.execute(
        "INSERT INTO event (node_seq, source, node_id, msg_type, msg_id, received_epoch, "
        "clock_trust, node_epoch, tag, fields, payload) VALUES "
        "(1, 'node', 1, ?, 10, 100.0, 'synced', 1000, ?, '{}', NULL)",
        (payloads.READS, TAG_A),
    )
    conn.execute(
        "INSERT INTO event (node_seq, source, node_id, msg_type, msg_id, received_epoch, "
        "clock_trust, node_epoch, tag, fields, payload) VALUES "
        "(2, 'node', 1, ?, 11, 101.0, 'synced', 1001, ?, '{}', NULL)",
        (payloads.CAPTURED, TAG_B),
    )
    conn.execute(
        "INSERT INTO event (node_seq, source, node_id, msg_type, msg_id, received_epoch, "
        "clock_trust, node_epoch, tag, fields, payload) VALUES "
        "(3, 'node', 1, ?, 12, 102.0, 'synced', 1002, NULL, '{}', NULL)",
        (payloads.HEARTBEAT,),
    )
    conn.execute(
        "INSERT INTO node_cursor (node_id, last_msg_id, last_seq, last_node_seq) "
        "VALUES (1, 12, 3, 3)"
    )
    conn.commit()
    conn.close()


def test_migration_backfills_specimens_from_a_database_written_before_the_table_existed(
    tmp_path: Path,
) -> None:
    """A migration proven only on an empty database is not proven -- this
    one runs against a file a schema-version-1 station already wrote real
    rows into, the same shape live bench data is in today.
    """
    db_path = tmp_path / "station.db"
    _write_pre_specimen_database(db_path)

    store = Store(db_path)  # today's Store, opened against yesterday's file

    tags = {row.tag for row in store.specimens()}
    assert tags == {TAG_A, TAG_B}  # the HEARTBEAT's NULL tag contributed nothing

    conn = store._conn
    version = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
    assert int(version) == SCHEMA_VERSION


def test_reopening_a_migrated_database_does_not_duplicate_or_re_scan(tmp_path: Path) -> None:
    """Once ``schema_version`` reaches 2, the backfill must not run again on
    every boot -- it is a one-time migration, not a recurring full-table
    scan of ``event`` paid on every restart of a station that may run for a
    season.
    """
    db_path = tmp_path / "station.db"
    _write_pre_specimen_database(db_path)

    Store(db_path).close()
    reopened = Store(db_path)

    tags = [row.tag for row in reopened.specimens()]
    assert sorted(tags) == sorted([TAG_A, TAG_B])  # stable, not duplicated


# --- Analysis shapes are views, not tables -----------------------------------


def test_last_seen_tracks_a_new_event_with_no_update_to_the_specimen_row(tmp_path: Path) -> None:
    """The required proof against the denormalised-``last_seen`` defect: a
    second read for a tag already known must change what ``specimens()``
    reports, even though nothing ever runs an ``UPDATE`` against the
    ``specimen`` row itself -- ``last_seen`` is computed from ``event`` on
    every call, never cached beside the roster.
    """
    store = Store(tmp_path / "station.db")

    store.append(
        _captured_event(node_id=1, msg_id=1, tag=TAG_A), received_epoch=100.0, clock_trust="synced"
    )
    first = next(row for row in store.specimens() if row.tag == TAG_A)
    assert first.last_seen_epoch == 100.0
    assert first.times_seen == 1

    store.append(
        _captured_event(node_id=1, msg_id=2, tag=TAG_A), received_epoch=250.0, clock_trust="synced"
    )
    second = next(row for row in store.specimens() if row.tag == TAG_A)
    assert second.last_seen_epoch == 250.0  # advanced with no write to `specimen` at all
    assert second.times_seen == 2


def test_wanted_tags_returns_only_of_interest_specimens_in_tag_order(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")
    for i, tag in enumerate((TAG_C, TAG_A, TAG_B)):
        store.append(
            _captured_event(node_id=1, msg_id=i + 1, tag=tag),
            received_epoch=100.0 + i,
            clock_trust="synced",
        )

    store.update_specimen(TAG_A, {"of_interest": True})
    store.update_specimen(TAG_C, {"of_interest": True})
    # TAG_B stays not-of-interest.

    assert store.wanted_tags() == sorted([TAG_A, TAG_C])


# --- Editing a specimen -------------------------------------------------------


def test_update_specimen_sets_species_and_band_number(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")
    store.append(
        _captured_event(node_id=1, msg_id=1, tag=TAG_A), received_epoch=100.0, clock_trust="synced"
    )

    updated = store.update_specimen(TAG_A, {"species": "Zonotrichia capensis", "band_number": "AB123"})

    assert updated is True
    row = store.specimens()[0]
    assert row.species == "Zonotrichia capensis"
    assert row.band_number == "AB123"
    assert row.of_interest is False  # untouched by this edit


def test_update_specimen_only_touches_fields_actually_given(tmp_path: Path) -> None:
    """A partial edit (only `of_interest`) must never blank out a
    previously-recorded species/band number -- the same "only apply what was
    actually given" discipline `TargetsRequest`'s own validator applies at
    the HTTP boundary.
    """
    store = Store(tmp_path / "station.db")
    store.append(
        _captured_event(node_id=1, msg_id=1, tag=TAG_A), received_epoch=100.0, clock_trust="synced"
    )
    store.update_specimen(TAG_A, {"species": "Zonotrichia capensis"})

    store.update_specimen(TAG_A, {"of_interest": True})

    row = store.specimens()[0]
    assert row.species == "Zonotrichia capensis"  # still there
    assert row.of_interest is True


def test_update_specimen_on_an_unknown_tag_reports_not_found(tmp_path: Path) -> None:
    """The roster grows from observed reads, never from an edit -- an
    operator can only mark a specimen the antenna has already produced
    (pending.md: "nothing has to be pre-registered", read the other way
    round: nothing may be pre-registered through this door either).
    """
    store = Store(tmp_path / "station.db")

    updated = store.update_specimen(TAG_A, {"of_interest": True})

    assert updated is False
    assert store.specimens() == []
