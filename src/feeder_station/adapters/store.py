"""Durable, restart-safe storage for everything a node reports (S9).

One append-only ``event`` log; every piece of trap state is *derived* from it
(design.md, DD2) — there is no mutable ``trap_state`` table, and nowhere to
write a state nobody reported. WAL, ``synchronous=FULL``: ``NORMAL`` stays
uncorrupted but can lose the most recently committed transaction on power
loss, which is exactly the event S9 says must survive.

There is deliberately no in-memory "mark this acknowledged" shortcut
anywhere in this module. The only way to record anything — an inbound
event, an operator's acknowledgement, a withheld ``TIME_SYNC`` — is
:meth:`Store.append`, which runs one ``BEGIN IMMEDIATE`` transaction to
completion before it returns. A caller cannot obtain a "go ahead and ACK"
signal any earlier than that return, because nothing else in this class
hands one out.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from feeder_station.domain import payloads
from feeder_station.domain.events import OPERATOR_CAPTURE_ACKNOWLEDGED, StationEvent
from feeder_station.domain.traps import Registry, TrapState

# DD6: a count-bounded per-node window, never a time window — dedup must not
# depend on the one thing S12 says the station may not trust: the clock.
# W=32768 needs 65536 events to alias two msg_ids into the window at once
# (the wire msg_id wraps at 2**16), a 2x margin.
DEDUP_WINDOW = 32768

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event (
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

CREATE INDEX IF NOT EXISTS event_dedup   ON event(node_id, msg_id, node_seq DESC);
CREATE INDEX IF NOT EXISTS event_by_type ON event(node_id, msg_type, seq DESC);

CREATE TABLE IF NOT EXISTS node_cursor (
    node_id       INTEGER PRIMARY KEY,
    last_msg_id   INTEGER NOT NULL,
    last_seq      INTEGER NOT NULL,
    last_node_seq INTEGER NOT NULL
);
"""

# Applied to every connection this station ever opens against station.db —
# the store's own, and (task 4.21, reused rather than repeated) slice 11's
# export connection later. See design.md section 2's PRAGMA table for the
# reasoning behind each value; `synchronous=FULL` is the one S9 depends on.
_PRAGMAS: tuple[tuple[str, str | int], ...] = (
    ("journal_mode", "WAL"),
    ("synchronous", "FULL"),
    ("busy_timeout", 5000),
    ("foreign_keys", "ON"),
    ("journal_size_limit", 4194304),
    ("trusted_schema", "OFF"),
)


def open_connection(path: str | Path) -> sqlite3.Connection:
    """Open one connection with the full PRAGMA set applied.

    ``isolation_level=None`` puts the connection in autocommit mode so
    :meth:`Store.append` can issue its own explicit ``BEGIN IMMEDIATE`` /
    ``COMMIT`` — the Python ``sqlite3`` module's own implicit-transaction
    handling would otherwise fight over when a transaction starts.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    for pragma, value in _PRAGMAS:
        conn.execute(f"PRAGMA {pragma} = {value}")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


@dataclass(frozen=True)
class Appended:
    seq: int
    node_seq: int | None
    duplicate_of: int | None  # DD8: a duplicate is acknowledged, not ignored


@dataclass(frozen=True)
class Cursor:
    last_msg_id: int
    last_seq: int
    last_node_seq: int


@dataclass(frozen=True)
class StoredEvent:
    seq: int
    node_seq: int | None
    source: str
    node_id: int
    msg_type: int
    msg_id: int | None
    received_epoch: float
    clock_trust: str
    node_epoch: int | None
    tag: bytes | None
    fields: dict[str, object]
    payload: bytes | None


_SELECT_COLUMNS = (
    "seq, node_seq, source, node_id, msg_type, msg_id, received_epoch, "
    "clock_trust, node_epoch, tag, fields, payload"
)


def _row_to_event(row: tuple[Any, ...]) -> StoredEvent:
    (
        seq,
        node_seq,
        source,
        node_id,
        msg_type,
        msg_id,
        received_epoch,
        clock_trust,
        node_epoch,
        tag,
        fields_json,
        payload,
    ) = row
    return StoredEvent(
        seq=seq,
        node_seq=node_seq,
        source=source,
        node_id=node_id,
        msg_type=msg_type,
        msg_id=msg_id,
        received_epoch=received_epoch,
        clock_trust=clock_trust,
        node_epoch=node_epoch,
        tag=tag,
        fields=json.loads(fields_json),
        payload=payload,
    )


def _epoch_to_datetime(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


# domain/payloads.py documents HEARTBEAT's `state` byte only as "assumption;
# small enum (0-3)" — messages.md does not pin an order. This assumes the
# wire order matches TrapState's own declaration order (IDLE, ARMED, FIRED,
# FAULT), the same kind of documented assumption payloads.py already makes
# for unpinned field widths. A messages.md revision that pins this
# explicitly wins over this assumption.
_WIRE_STATE_ORDER = (TrapState.IDLE, TrapState.ARMED, TrapState.FIRED, TrapState.FAULT)


def trap_state_from_wire(value: int) -> TrapState:
    try:
        return _WIRE_STATE_ORDER[value]
    except IndexError as exc:
        raise ValueError(f"HEARTBEAT state byte {value} has no known TrapState") from exc


class Store:
    """The station's own event log, and everything derived from it."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn = open_connection(self._path)
        _ensure_schema(self._conn)

    def close(self) -> None:
        self._conn.close()

    def integrity_check(self) -> str:
        row = self._conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0])

    def append(self, event: StationEvent, *, received_epoch: float, clock_trust: str) -> Appended:
        """Persist one event, including its fsync, before returning.

        One ``BEGIN IMMEDIATE`` transaction: insert the row, upsert
        ``node_cursor`` for node-sourced events, commit. Nothing about this
        call is deferred — the caller may not treat any return from this
        method as "in flight"; by the time it has a value, the write and its
        fsync (``synchronous=FULL``) are already durable.
        """
        if event.source == "node" and event.msg_id is None:
            raise ValueError("a node-sourced event must carry a msg_id")

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            node_seq, duplicate_of = self._next_node_seq_and_dedup(conn, event)
            if duplicate_of is not None:
                conn.execute("COMMIT")
                return Appended(seq=duplicate_of, node_seq=None, duplicate_of=duplicate_of)

            cursor = conn.execute(
                """
                INSERT INTO event (
                    node_seq, source, node_id, msg_type, msg_id,
                    received_epoch, clock_trust, node_epoch, tag, fields, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_seq,
                    event.source,
                    event.node_id,
                    event.msg_type,
                    event.msg_id,
                    received_epoch,
                    clock_trust,
                    event.node_epoch,
                    event.tag,
                    json.dumps(dict(event.fields)),
                    event.payload,
                ),
            )
            seq = cursor.lastrowid
            assert seq is not None

            if event.source == "node":
                assert node_seq is not None
                assert event.msg_id is not None
                conn.execute(
                    """
                    INSERT INTO node_cursor (node_id, last_msg_id, last_seq, last_node_seq)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(node_id) DO UPDATE SET
                        last_msg_id   = excluded.last_msg_id,
                        last_seq      = excluded.last_seq,
                        last_node_seq = excluded.last_node_seq
                    """,
                    (event.node_id, event.msg_id, seq, node_seq),
                )

            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

        return Appended(seq=seq, node_seq=node_seq, duplicate_of=None)

    def _next_node_seq_and_dedup(
        self, conn: sqlite3.Connection, event: StationEvent
    ) -> tuple[int | None, int | None]:
        """DD6/DD8: bounded-window dedup, keyed on the station's own node_seq.

        Returns ``(node_seq, duplicate_of)``. ``node_seq`` is ``None`` for
        anything not sourced from a node (design.md: "NULL when source <>
        'node'") and also ``None`` when a duplicate is found, since a
        duplicate consumes no new ordinal.
        """
        if event.source != "node":
            return None, None

        row = conn.execute(
            "SELECT last_node_seq FROM node_cursor WHERE node_id = ?", (event.node_id,)
        ).fetchone()
        previous_node_seq = row[0] if row is not None else 0

        duplicate = conn.execute(
            """
            SELECT seq FROM event
             WHERE node_id = ? AND msg_id = ? AND node_seq > ?
             ORDER BY node_seq DESC LIMIT 1
            """,
            (event.node_id, event.msg_id, previous_node_seq - DEDUP_WINDOW),
        ).fetchone()
        if duplicate is not None:
            return None, duplicate[0]

        return previous_node_seq + 1, None

    def cursor(self, node_id: int) -> Cursor | None:
        row = self._conn.execute(
            "SELECT last_msg_id, last_seq, last_node_seq FROM node_cursor WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        return Cursor(last_msg_id=row[0], last_seq=row[1], last_node_seq=row[2])

    def latest(self, node_id: int, msg_type: int) -> StoredEvent | None:
        row = self._conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS} FROM event
             WHERE node_id = ? AND msg_type = ?
             ORDER BY seq DESC LIMIT 1
            """,
            (node_id, msg_type),
        ).fetchone()
        return None if row is None else _row_to_event(row)

    def events_since(self, node_id: int, after_seq: int) -> list[StoredEvent]:
        """Events for ``node_id`` with store sequence greater than ``after_seq``,
        oldest first — the backlog cursor query the event-store spec names.
        """
        rows = self._conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS} FROM event
             WHERE node_id = ? AND seq > ?
             ORDER BY seq ASC
            """,
            (node_id, after_seq),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def rehydrate(self, registry: Registry) -> None:
        """Rebuild ``registry``'s per-node state from stored events.

        Design.md section 6's order: latest heartbeat, then the latest
        capture if no later acknowledgement exists for it. Reachability is
        deliberately never rehydrated — a restarted station has not heard
        from anyone yet (S11).

        The design's own prose names a ``traps.rehydrate()`` entry point,
        but ``domain/traps.py`` is not modified until slices 6 and 8; this
        method drives the registry through its existing public
        ``heartbeat()``/``captured()`` methods instead, which already do
        everything section 6 describes. The "desired command state" step it
        also describes is not implemented here: nothing before slice 8's
        ``domain/commands.py`` model exists to persist a desired command as
        a store row, so there is nothing yet to replay for that step. See
        this slice's apply-progress notes.
        """
        for node_id in self._known_node_ids():
            self._rehydrate_heartbeat(node_id, registry)
            self._rehydrate_capture(node_id, registry)

    def _known_node_ids(self) -> list[int]:
        rows = self._conn.execute("SELECT node_id FROM node_cursor ORDER BY node_id").fetchall()
        return [row[0] for row in rows]

    def _rehydrate_heartbeat(self, node_id: int, registry: Registry) -> None:
        heartbeat = self.latest(node_id, payloads.HEARTBEAT)
        if heartbeat is None:
            return

        state_value = heartbeat.fields.get("state")
        if not isinstance(state_value, int):
            return

        battery_mv = heartbeat.fields.get("battery_mv")
        targets_version = heartbeat.fields.get("targets_version")
        registry.heartbeat(
            node_id=node_id,
            state=trap_state_from_wire(state_value),
            at=_epoch_to_datetime(heartbeat.received_epoch),
            battery_mv=battery_mv if isinstance(battery_mv, int) else None,
            targets_version=targets_version if isinstance(targets_version, int) else None,
        )

    def _rehydrate_capture(self, node_id: int, registry: Registry) -> None:
        captured = self.latest(node_id, payloads.CAPTURED)
        if captured is None or captured.tag is None:
            return

        acknowledged = self.latest(node_id, OPERATOR_CAPTURE_ACKNOWLEDGED)
        if acknowledged is not None and acknowledged.seq > captured.seq:
            return

        registry.captured(
            node_id=node_id,
            tag=captured.tag,
            at=_epoch_to_datetime(captured.received_epoch),
        )
