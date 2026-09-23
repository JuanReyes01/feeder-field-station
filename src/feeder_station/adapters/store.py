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
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from feeder_station.domain import payloads
from feeder_station.domain.commands import Command, CommandKind, ReportedState
from feeder_station.domain.events import (
    OPERATOR_ARM,
    OPERATOR_CAPTURE_ACKNOWLEDGED,
    OPERATOR_DISARM,
    OPERATOR_SET_TARGETS,
    StationEvent,
)
from feeder_station.domain.traps import Registry, TrapState

# DD6: a count-bounded per-node window, never a time window — dedup must not
# depend on the one thing S12 says the station may not trust: the clock.
# W=32768 needs 65536 events to alias two msg_ids into the window at once
# (the wire msg_id wraps at 2**16), a 2x margin.
DEDUP_WINDOW = 32768

SCHEMA_VERSION = 1

# DD13: `Store.snapshot_into`'s `VACUUM INTO` (adapters/export.py, D6) needs
# SQLite 3.27 or newer. Checked once, at station startup — a station booting
# on a too-old SQLite fails loudly here instead of silently at the first
# export attempt, months later, on removable media.
_MIN_SQLITE_VERSION_FOR_VACUUM_INTO = (3, 27)


def _assert_vacuum_into_is_supported() -> None:
    if sqlite3.sqlite_version_info < _MIN_SQLITE_VERSION_FOR_VACUUM_INTO:
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} is older than 3.27; VACUUM INTO "
            "(the only export path D6 allows) is not available"
        )


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


def open_connection(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open one connection with the full PRAGMA set applied.

    ``isolation_level=None`` puts the connection in autocommit mode so
    :meth:`Store.append` can issue its own explicit ``BEGIN IMMEDIATE`` /
    ``COMMIT`` — the Python ``sqlite3`` module's own implicit-transaction
    handling would otherwise fight over when a transaction starts.

    ``check_same_thread=False`` is for :class:`Store`'s own per-thread
    connections alone (design.md section 7): each is only ever *used* from
    the one thread that lazily opened it, but the shell may need to
    *close* it later from whichever thread is running shutdown, after
    every worker thread has already been joined and stopped touching it.
    """
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=check_same_thread)
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
        _assert_vacuum_into_is_supported()
        self._path = Path(path)
        self._local = threading.local()
        self._opened_connections: list[sqlite3.Connection] = []
        self._opened_connections_lock = threading.Lock()
        # CRITICAL 2 (sdd-verify, field-station-p4): the running count of
        # rows `rehydrate()` could not read, reset at the start of each
        # rehydration pass. Mirrors `SerialGateway.link_state.decode_errors`
        # -- an attribute on the owning adapter, not a value threaded back
        # through a return the caller might discard.
        self.rehydration_errors = 0
        _ensure_schema(self._conn)

    @property
    def _conn(self) -> sqlite3.Connection:
        """design.md section 7's own concurrency table, verbatim: "One per
        thread, created lazily, each opened with the full PRAGMA set...
        `check_same_thread` exists for a reason." Before slice 12, exactly
        one thread — whichever constructed this ``Store`` — ever called
        into it. Slice 12 gives it three (the HTTP threadpool, the gateway
        thread, the scheduler thread); this lazily opens one connection
        per calling thread instead of sharing one, which sqlite3's own
        ``check_same_thread`` default would otherwise refuse outright.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_connection(self._path, check_same_thread=False)
            self._local.conn = conn
            with self._opened_connections_lock:
                self._opened_connections.append(conn)
        return conn

    def optimize(self) -> None:
        """design.md section 7's shutdown row, verbatim: "runs `PRAGMA
        optimize`, closes." SQLite's own recommendation is to run this
        once, right before closing a long-lived connection, so the query
        planner's statistics stay useful across a restart — cheap, and
        never called mid-session where it could momentarily block a writer.
        """
        self._conn.execute("PRAGMA optimize")

    def close(self) -> None:
        """Closes every connection this ``Store`` has ever lazily opened,
        across every thread that ever called into it — not only the
        calling thread's own.

        Safe only once every other thread holding a reference to this
        ``Store`` has actually stopped running (design.md section 7's
        "Shutdown" row: the lifespan joins both worker threads *before*
        calling this) — ``check_same_thread=False`` on each connection
        (see :func:`open_connection`) is what makes closing them from here,
        rather than from their own now-finished threads, possible at all.
        """
        with self._opened_connections_lock:
            connections, self._opened_connections = self._opened_connections, []
        for conn in connections:
            conn.close()
        if hasattr(self._local, "conn"):
            del self._local.conn

    def integrity_check(self) -> str:
        row = self._conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0])

    def snapshot_into(self, path: str | Path) -> None:
        """A consistent point-in-time copy of the live database (D6, DD13).

        One ``VACUUM INTO`` statement: SQLite opens a read transaction
        against this connection and copies every row committed as of that
        instant — including anything still only in ``station.db-wal`` and
        never yet checkpointed into ``station.db`` itself — into a fresh,
        compacted file at ``path``. Nothing about the live file, or any
        other connection against it, is touched; nothing is locked for
        longer than the copy itself takes.

        This is the *only* path an export may take (D6): a plain filesystem
        copy of a live WAL-mode database can land mid-checkpoint and
        silently omit rows S9 already promised were durable. See
        ``adapters/export.py`` and
        ``tests/adapters/test_export.py::test_export_under_concurrent_writes_omits_nothing_acknowledged``
        for the proof.
        """
        self._conn.execute("VACUUM INTO ?", (str(path),))

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

        Design.md section 6's three-step order, all now implemented
        (CRITICAL 1/2/verify-report's superseding note, sdd-verify
        field-station-p4 — the "desired command state" step this
        docstring used to call out as missing is step 3 below):

        1. Latest ``HEARTBEAT`` → the reported state, battery,
           ``targets_version`` and ``last_seen``.
        2. Latest unacknowledged ``CAPTURED`` → the capture alone
           (:meth:`Registry.restore_capture`, CRITICAL 2 — this must never
           also touch ``state``/``last_seen``: those are step 1's fact,
           however much older or newer the capture itself is).
        3. Latest ``operator/arm|disarm|set_targets`` → the *desired*
           state, replayed only if step 1's own heartbeat has not already
           satisfied it (DD15) — an edit the trap already confirmed before
           the restart must not be resurrected as pending.
           ``desired_tags`` (S2) is replayed on its own from the latest
           ``OPERATOR_SET_TARGETS`` row regardless, the same independence
           :meth:`Registry.command_set_targets` already gives it live.

        Reachability is deliberately never rehydrated — a restarted
        station has not heard from anyone yet (S11).

        Called from ``app.py``'s lifespan, before the HTTP server accepts
        (design.md section 6) — CRITICAL 1 (sdd-verify): this method had no
        production caller at all before that wiring existed; every test
        that exercised it fed an already-populated registry to
        ``create_app`` by hand instead.

        **Skip-and-count, never raise (CRITICAL 2, sdd-verify
        field-station-p4).** Each of a node's three replay steps runs
        under its own ``try``: a row this method cannot interpret —
        ``payloads.decode_heartbeat``/``decode_captured`` already reject an
        out-of-range ``state``/``state_after`` byte at write time (task
        1.2), but an older row written before that check existed, or any
        future row the check did not anticipate, can still be sitting in
        ``station.db`` — is skipped and counted in
        :attr:`rehydration_errors` instead of propagating out of this
        method. The same best-effort discipline
        ``adapters/ingest.py::_apply_to_registry`` already applies to the
        live path: one bad row must never cost every other node its state,
        and it must never cost the station its boot. A restart that cannot
        read one row must still be a restart — ``event-store/spec.md``'s
        "Registry rehydration at boot" requirement, and
        ``station-deployment/spec.md``'s S7 ("starts by itself... a power
        cut must not need a person"), which a rehydration-time crash loop
        directly violates.
        """
        self.rehydration_errors = 0
        for node_id in self._known_node_ids():
            for step in (
                self._rehydrate_heartbeat,
                self._rehydrate_capture,
                self._rehydrate_desired_command,
            ):
                try:
                    step(node_id, registry)
                except ValueError:
                    # `ValueError` covers both `payloads.PayloadError` (a
                    # future row this method itself decodes) and
                    # `trap_state_from_wire`'s own raise for a
                    # pre-existing out-of-range row — the same family
                    # `_require_len` et al. already raise for a malformed
                    # payload (task 1.2's decode-error type, reused here
                    # rather than invented fresh).
                    self.rehydration_errors += 1

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

        # CRITICAL 2: `restore_capture`, never `captured()` — the latter
        # also stamps `state`/`last_seen`, which must stay the heartbeat
        # replay's fact alone, regardless of which of the two is newer.
        registry.restore_capture(
            node_id=node_id,
            tag=captured.tag,
            at=_epoch_to_datetime(captured.received_epoch),
        )

    _OPERATOR_COMMAND_MSG_TYPES: tuple[tuple[int, CommandKind], ...] = (
        (OPERATOR_ARM, CommandKind.ARM),
        (OPERATOR_DISARM, CommandKind.DISARM),
        (OPERATOR_SET_TARGETS, CommandKind.SET_TARGETS),
    )

    def _rehydrate_desired_command(self, node_id: int, registry: Registry) -> None:
        """Design.md section 6's third replay step."""
        latest_targets = self.latest(node_id, OPERATOR_SET_TARGETS)
        if latest_targets is not None:
            tags = _tags_from_fields(latest_targets.fields)
            if tags is not None:
                registry.restore_desired_tags(node_id, tags)

        latest: StoredEvent | None = None
        latest_kind: CommandKind | None = None
        for msg_type, kind in self._OPERATOR_COMMAND_MSG_TYPES:
            row = self.latest(node_id, msg_type)
            if row is not None and (latest is None or row.seq > latest.seq):
                latest, latest_kind = row, kind
        if latest is None or latest_kind is None:
            return

        targets_version: int | None = None
        if latest_kind is CommandKind.SET_TARGETS:
            version = latest.fields.get("targets_version")
            if not isinstance(version, int):
                return
            targets_version = version

        if Command(kind=latest_kind, targets_version=targets_version).satisfied_by(
            _reported_state(registry, node_id)
        ):
            # The trap's own already-rehydrated heartbeat confirms this
            # command was answered before the restart (DD15) — nothing left
            # to resend.
            return

        at = _epoch_to_datetime(latest.received_epoch)
        if latest_kind is CommandKind.ARM:
            registry.command_arm(node_id, at=at)
        elif latest_kind is CommandKind.DISARM:
            registry.command_disarm(node_id, at=at)
        else:
            assert targets_version is not None  # narrowed above: SET_TARGETS always sets this
            tags = _tags_from_fields(latest.fields) or ()
            registry.command_set_targets(node_id, targets_version=targets_version, at=at, tags=tags)


def _reported_state(registry: Registry, node_id: int) -> ReportedState:
    try:
        trap = registry.trap(node_id)
    except KeyError:
        return ReportedState(armed=False, targets_version=None)
    return ReportedState(armed=trap.state is TrapState.ARMED, targets_version=trap.targets_version)


def _tags_from_fields(fields: dict[str, object]) -> tuple[bytes, ...] | None:
    raw_tags = fields.get("tags")
    if not isinstance(raw_tags, list):
        return None
    tags: list[bytes] = []
    for item in raw_tags:
        if not isinstance(item, str):
            return None
        tags.append(bytes.fromhex(item))
    return tuple(tags)
