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
import logging
import sqlite3
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, NoReturn

from feeder_station.domain import payloads
from feeder_station.domain.commands import Command, CommandKind, ReportedState
from feeder_station.domain.events import (
    OPERATOR_ARM,
    OPERATOR_CANCEL_COMMAND,
    OPERATOR_CAPTURE_ACKNOWLEDGED,
    OPERATOR_DISARM,
    OPERATOR_SET_TARGETS,
    StationEvent,
    tags_from_fields,
)
from feeder_station.domain.traps import Registry, TrapState

# DD6: a count-bounded per-node window, never a time window — dedup must not
# depend on the one thing S12 says the station may not trust: the clock.
# W=32768 needs 65536 events to alias two msg_ids into the window at once
# (the wire msg_id wraps at 2**16), a 2x margin.
#
# This margin is counted in messages, not rows: a READS batch's up to 17
# reads share one msg_id and therefore one node_seq step
# (Store.append_batch), never one node_seq per read. Advancing node_seq
# once per row instead would let a single batch consume up to 17 steps of
# window headroom for one message, shrinking the 2x margin above by up to
# that same factor for no reason connected to how many distinct messages
# actually arrived.
DEDUP_WINDOW = 32768

# HELLO.boot_count (docs/protocol/messages.md) is a 2-byte LE field, the same
# width as msg_id -- and, exactly like msg_id (DEDUP_WINDOW's own comment
# above), it wraps. `_boot_count_is_newer` below is a modular "greater than",
# not `candidate > stored`: a plain `>` reads a genuine reboot that rolls the
# counter from 65535 back to 0 as *older*, and would refuse to reset dedup on
# precisely the boot where the wrap makes the restart's own msg_ids collide
# hardest with what the previous boot already stored.
_BOOT_COUNT_MODULUS = 1 << 16


def _boot_count_is_newer(candidate: int, stored: int) -> bool:
    """Whether ``candidate`` is a later boot than ``stored``, mod 65536.

    Classic serial-number arithmetic (RFC 1982), the same shape DEDUP_WINDOW's
    own "2x margin" comment already reasons about for msg_id: the forward
    distance from ``stored`` to ``candidate`` is ``newer`` when it falls in
    the closer half of the ring (``0 <`` distance ``< 32768``) and not newer
    otherwise -- ``0`` (equal; the expected HELLO retry) and the far half
    (``candidate`` is actually the *older* value, wrapped) both read as "not
    newer". A node reboots far too rarely for genuinely-newer and
    genuinely-older to alias across that half-ring boundary -- the same order
    of margin DEDUP_WINDOW already leans on for msg_id.
    """
    forward_distance = (candidate - stored) % _BOOT_COUNT_MODULUS
    return 0 < forward_distance < _BOOT_COUNT_MODULUS // 2


SCHEMA_VERSION = 6

logger = logging.getLogger(__name__)

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


_SCHEMA = f"""
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

-- `boot_count` (SCHEMA_VERSION 3 migration) and `reported_targets_version`/
-- `reported_tags` (SCHEMA_VERSION 4 migration, below) are each added by
-- `ALTER TABLE`, never listed here: this `CREATE TABLE IF NOT EXISTS` also
-- runs, unchanged, against a database a SCHEMA_VERSION-3+ (or 4+) station
-- already wrote -- if the columns were declared here too, that would leave
-- the *migration* as the only path that ever adds them for a database that
-- predates them, while a brand-new database would need them created a
-- second, different way. Adding each exclusively through its own gated
-- migration keeps there being exactly one path, proven against exactly the
-- pre-existing-data shape live bench databases are already in.
CREATE TABLE IF NOT EXISTS node_cursor (
    node_id       INTEGER PRIMARY KEY,
    last_msg_id   INTEGER NOT NULL,
    last_seq      INTEGER NOT NULL,
    last_node_seq INTEGER NOT NULL
);

-- SCHEMA_VERSION 2 (docs/pending.md, "The station has one kind of storage
-- and needs two"): observations are immutable and already fit `event`;
-- a specimen is mutable reference data and does not fit an append-only log
-- at all -- it needs its own table. `tag` alone is the primary key, not
-- `(node_id, tag)`: "of interest is a flag on the specimen, not a list per
-- feeder" -- the same animal seen at two feeders is one row, not two.
-- `species`/`band_number` are the only editable fields beyond the flag
-- (pending.md names exactly these two; no wider taxonomy is invented here).
CREATE TABLE IF NOT EXISTS specimen (
    tag         BLOB PRIMARY KEY,
    species     TEXT,
    band_number TEXT,
    of_interest INTEGER NOT NULL DEFAULT 0
);

-- SCHEMA_VERSION 5: a subject can be of interest in every trap
-- (`specimen.of_interest`, which also covers traps connected later) or in
-- specific ones (this table). What a trap hunts is the union of the two.
CREATE TABLE IF NOT EXISTS specimen_trap_interest (
    tag     BLOB    NOT NULL,
    node_id INTEGER NOT NULL,
    PRIMARY KEY (tag, node_id)
);

-- SCHEMA_VERSION 6: the days a subject appears on in the CICG's list (its
-- marking and every recapture), as ISO dates. Filled only by importing the
-- list; the operator filters by them.
CREATE TABLE IF NOT EXISTS specimen_capture_day (
    tag BLOB NOT NULL,
    day TEXT NOT NULL,
    PRIMARY KEY (tag, day)
);

-- SCHEMA_VERSION 5: what the station knows about a trap that the trap cannot
-- say itself. Traps join on their own; this row appears only once an operator
-- has said something about one.
CREATE TABLE IF NOT EXISTS trap_profile (
    node_id    INTEGER PRIMARY KEY,
    name       TEXT,
    has_latch  INTEGER,
    setup_done INTEGER NOT NULL DEFAULT 0
);

-- "Analysis shapes should be views, not tables" (pending.md): `times_seen`/
-- `last_seen` must never be columns on `specimen` itself -- a stored count
-- or timestamp there is exactly the "state that was true once, presented as
-- true now" defect this project has hit eight times. A real SQL VIEW, not a
-- column, so it structurally cannot fall out of sync with `event`: there is
-- no second copy to disagree with the first. Scoped to the two message
-- types that ever carry a real animal tag (`READS`, `CAPTURED` --
-- `domain/events.py::_tag_of`/`interpret_reads`); an operator's own
-- commands and a node's HEARTBEAT/FAULT never do.
CREATE VIEW IF NOT EXISTS specimen_activity AS
SELECT tag,
       COUNT(*)            AS times_seen,
       MAX(received_epoch) AS last_seen_epoch
  FROM event
 WHERE tag IS NOT NULL AND msg_type IN ({payloads.READS}, {payloads.CAPTURED})
 GROUP BY tag;
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
    """``CREATE TABLE``/``CREATE VIEW ... IF NOT EXISTS`` handles adding a
    brand-new object safely on a database that already has rows -- but a
    new table starts empty, and the roster's own rule (pending.md: "the
    first appearance of an unknown tag *is* a new specimen") applies just
    as much to a tag this station read *before* ``specimen`` existed. The
    one-time backfill below is what makes that retroactive, proven against
    exactly that shape by
    ``tests/adapters/test_specimens.py::test_migration_backfills_specimens_from_a_database_written_before_the_table_existed``.

    Gated on the *stored* ``schema_version``, not on whether ``specimen``
    happens to be empty right now: the previous version of this function's
    own upsert (``INSERT OR IGNORE``) never actually advanced
    ``schema_version`` past its initial value, which is corrected here too
    (``ON CONFLICT ... DO UPDATE``) -- without a real version to gate on,
    every boot would re-run a full scan of ``event`` looking for untracked
    tags, a cost that grows with the log for as long as this station runs.

    **SCHEMA_VERSION 3** (docs/pending.md, "A node goes invisible after
    every power cut"): ``node_cursor`` gains a ``boot_count`` column, the
    per-node high-water mark ``_next_node_seq_and_dedup`` compares a
    ``HELLO``'s own ``boot_count`` against to decide whether to reset that
    node's dedup window. ``ALTER TABLE ... ADD COLUMN`` is not idempotent
    the way ``CREATE TABLE IF NOT EXISTS`` is -- run twice, the second call
    raises ``sqlite3.OperationalError`` for a column that already exists --
    so this, unlike the column-free tables above, genuinely needs the same
    stored-version gate the specimen backfill already established, not a
    second attempt at an ``IF NOT EXISTS`` idiom SQLite's ``ALTER TABLE``
    does not offer for columns. Proven against a database a
    SCHEMA_VERSION-2 station already wrote real ``node_cursor`` rows into
    by
    ``tests/adapters/test_hello_resets_dedup.py::test_migration_adds_boot_count_to_an_existing_node_cursor_row``,
    and proven not to re-run on a second boot by that same module's
    ``test_reopening_a_migrated_database_does_not_re_run_the_migration``.

    **SCHEMA_VERSION 4** (docs/protocol/messages.md, `GET_TARGETS`/
    `TARGETS`): `node_cursor` gains `reported_targets_version`/
    `reported_tags`, the trap's last fully-reassembled `TARGETS` reply --
    `Store.record_reported_targets`'s own cache, read back by `Store.
    reported_targets` at rehydration. Both columns are nullable and
    neither is backfilled: a database written before this migration has
    no history to backfill from (this pair of messages did not exist
    yet), so every pre-existing row simply starts at the same honest
    "never reported" a brand-new row would, exactly as `boot_count`
    started every pre-existing row at its own default rather than
    guessing a history it does not have.
    """
    conn.executescript(_SCHEMA)

    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    previous_version = int(row[0]) if row is not None else 0
    if previous_version < 2:
        conn.execute(
            "INSERT OR IGNORE INTO specimen (tag) "
            "SELECT DISTINCT tag FROM event WHERE tag IS NOT NULL"
        )
    if previous_version < 3:
        conn.execute("ALTER TABLE node_cursor ADD COLUMN boot_count INTEGER NOT NULL DEFAULT 0")
    if previous_version < 4:
        conn.execute("ALTER TABLE node_cursor ADD COLUMN reported_targets_version INTEGER")
        conn.execute("ALTER TABLE node_cursor ADD COLUMN reported_tags TEXT")
    if previous_version < 5:
        # A tag is only a known subject once someone says so. Every tagged bird
        # is on the CICG's list, so a read of a tag nobody registered is
        # recorded but shown as an unknown reading. Anything an operator already
        # described or marked before this version counts as registered.
        conn.execute("ALTER TABLE specimen ADD COLUMN species_code TEXT")
        conn.execute("ALTER TABLE specimen ADD COLUMN sex TEXT")
        conn.execute("ALTER TABLE specimen ADD COLUMN age TEXT")
        conn.execute("ALTER TABLE specimen ADD COLUMN registered INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            "UPDATE specimen SET registered = 1 "
            "WHERE species IS NOT NULL OR band_number IS NOT NULL OR of_interest = 1"
        )

    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


@dataclass(frozen=True)
class Appended:
    seq: int
    node_seq: int | None
    duplicate_of: int | None  # DD8: a duplicate is acknowledged, not ignored


@dataclass(frozen=True)
class AppendedBatch:
    """:meth:`Store.append_batch`'s own return shape — see its docstring.

    ``seqs`` is empty exactly when ``duplicate_of`` is set (the whole frame
    was a repeat; nothing new was inserted); otherwise it holds one store
    ``seq`` per row inserted, in the order given, all sharing the one
    ``node_seq`` this frame's dedup check assigned.
    """

    seqs: tuple[int, ...]
    node_seq: int | None
    duplicate_of: int | None


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


@dataclass(frozen=True)
class SpecimenRow:
    """One row of the roster, as :meth:`Store.specimens` reports it.

    ``times_seen``/``last_seen_epoch`` are never stored -- they come from
    the ``specimen_activity`` view (see ``_SCHEMA``'s own comment), computed
    fresh on every call, so this dataclass can only ever show what the event
    log currently says, never a cached answer that could fall behind it.
    """

    tag: bytes
    species: str | None
    band_number: str | None
    of_interest: bool
    times_seen: int
    last_seen_epoch: float | None
    species_code: str | None = None
    sex: str | None = None
    age: str | None = None
    registered: bool = False
    trap_ids: tuple[int, ...] = ()
    capture_days: tuple[str, ...] = ()  # ISO dates from the CICG's list, oldest first


@dataclass(frozen=True)
class CaptureRow:
    """One trap release and what the operator found, if they have said."""

    node_id: int
    tag: bytes | None
    epoch: float
    result: str | None


@dataclass(frozen=True)
class TrapProfile:
    node_id: int
    name: str | None = None
    has_latch: bool | None = None
    setup_done: bool = False


# The only fields an operator may edit on a specimen (pending.md: "keep the
# set small and obvious; do not invent a taxonomy"). A fixed allowlist,
# never the caller's own dict keys interpolated straight into SQL --
# `Store.update_specimen` builds its `UPDATE ... SET` clause from this tuple
# alone, so an unexpected key in `fields` is silently not applied rather
# than becoming a column name.
_SPECIMEN_EDITABLE_FIELDS = (
    "species",
    "species_code",
    "sex",
    "age",
    "band_number",
    "of_interest",
    "registered",
)
_SPECIMEN_DESCRIBING_FIELDS = ("species", "species_code", "sex", "age", "band_number")


@dataclass(frozen=True)
class _ReportedFact:
    """One state-bearing row's candidate for `rehydrate`'s state race
    (CRITICAL 1, sdd-verify field-station-p4 batch 2): whichever of a
    node's HEARTBEAT/CAPTURED/FAULT rows has the highest `seq` is its
    most recent report and wins, regardless of type -- the same "any of
    the three is a report" reading `ingest.py::_apply_to_registry`
    already gives the live path (`registry.heartbeat()`/`captured()`/
    `fault()` each set `state` unconditionally).

    ``msg_type`` (BEHAVIOUR 4 / SUGGESTION 5, verify-report-batch2d.md):
    which report produced this fact, so `_rehydrate_desired_command` can
    tell a HEARTBEAT/CAPTURED win (a genuinely reported fact, may satisfy
    a pending command, mirroring `Registry.heartbeat`/`captured`) from a
    FAULT win (no state field of its own, must never satisfy one,
    mirroring `Registry.fault`'s own deliberate omission).
    """

    seq: int
    state: TrapState
    at: datetime
    msg_type: int


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
        # BEHAVIOUR 2 (sdd-verify field-station-p4, verify-report-batch2d.md):
        # Python's own negative indexing let `-1`..`-4` resolve to
        # `FAULT`/`FIRED`/`ARMED`/`IDLE` instead of raising -- `[-4]`
        # fabricated the calmest possible answer for a byte that could
        # only reach here through corruption (the wire path is already
        # rejected by `payloads._require_valid_state`). Raised through the
        # same `IndexError` branch below rather than a second message.
        if value < 0:
            raise IndexError(value)
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
        # WARNING K(b) (sdd-verify field-station-p4 batch 2,
        # verify-report-batch1-round2.md): `rehydration_errors` alone reads
        # identically for "one bad row, roster otherwise intact" and "the
        # roster query itself died, registry empty" -- a degraded boot, not
        # an error count. Set only in that second branch, reset at the
        # start of each rehydration pass.
        self.roster_lost = False
        # WARNING 4 (verify-report-batch2.md): `roster_lost` only catches a
        # roster *query* that raises -- a query that succeeds while
        # returning fewer nodes than the store actually holds (a
        # `node_cursor` row lost to a partial write, its node's other rows
        # otherwise intact) reads identically to a fully healthy boot.
        # `roster_nodes_expected` is `_count_all_known_nodes()`'s own
        # source-agnostic count straight off `event` -- the ground truth
        # `_known_node_ids()`'s own union is supposed to match; `roster_
        # nodes_rehydrated` is how many `_known_node_ids()` actually
        # returned. Both reset at the start of each rehydration pass.
        self.roster_nodes_expected = 0
        self.roster_nodes_rehydrated = 0
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
            node_seq, duplicate_of, hello_boot_count = self._next_node_seq_and_dedup(conn, event)
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

            # pending.md, "the first appearance of an unknown tag IS a new
            # specimen": inside this same BEGIN IMMEDIATE, not a second
            # write after COMMIT. A separate autocommit statement would cost
            # this station a second fsync per event (`synchronous=FULL`)
            # for no reason connected to what changed; folded in here, the
            # roster becomes durable in the exact same fsync S9 already
            # pays for, and can never be left half-caught-up by a crash
            # between "the read landed" and "the roster noticed."
            if event.tag is not None:
                conn.execute("INSERT OR IGNORE INTO specimen (tag) VALUES (?)", (event.tag,))

            if event.source == "node":
                assert node_seq is not None
                assert event.msg_id is not None
                if hello_boot_count is not None:
                    # A HELLO -- also persist this node's boot_count
                    # high-water mark (docs/pending.md, "A node goes
                    # invisible after every power cut"; see
                    # `_next_node_seq_and_dedup`'s own docstring). Every
                    # other message type keeps the plain upsert below
                    # completely untouched, never referencing this column at
                    # all, so it is never overwritten with a value the wire
                    # never carried.
                    conn.execute(
                        """
                        INSERT INTO node_cursor (
                            node_id, last_msg_id, last_seq, last_node_seq, boot_count
                        )
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(node_id) DO UPDATE SET
                            last_msg_id   = excluded.last_msg_id,
                            last_seq      = excluded.last_seq,
                            last_node_seq = excluded.last_node_seq,
                            boot_count    = excluded.boot_count
                        """,
                        (event.node_id, event.msg_id, seq, node_seq, hello_boot_count),
                    )
                else:
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

    def append_batch(
        self, events: Sequence[StationEvent], *, received_epoch: float, clock_trust: str
    ) -> AppendedBatch:
        """Persist every event in ``events`` as the many rows of one frame.

        Generalises :meth:`append` for exactly the one case that method
        cannot safely be reused for unmodified: a ``READS`` batch, where
        ``count`` reads all arrive inside one frame and therefore all share
        one wire ``msg_id`` (``domain/events.py::interpret_reads`` is the
        one caller that produces such a sequence, one call per frame).

        **Why this cannot be ``for event in events: self.append(event, ...)``.**
        :meth:`append`'s own dedup (:meth:`_next_node_seq_and_dedup`) keys on
        ``(node_id, msg_id)`` alone — correct when one event owns one
        msg_id, but a batch's second read shares the *first* read's msg_id.
        A naive per-event loop would insert read 1, advance
        ``node_cursor``, and then see every remaining read as a repeat of
        the message it just stored — 16 of 17 reads gone, silently, with no
        error and no log line, because :meth:`append` (DD8) treats a
        duplicate as a thing to acknowledge quietly, not to raise about.
        This method exists specifically so that never happens: the dedup
        check below runs exactly once per call, keyed on ``events[0]``'s
        ``(node_id, msg_id)``, before any row is inserted.

        **One ``node_seq`` for the whole batch, not one per row.**
        ``node_seq`` is this store's own bounded-window dedup counter
        (``DEDUP_WINDOW``'s own comment); every row this call inserts
        carries the *same* ``node_seq`` — see that comment for why
        advancing it once per row instead would shrink the window's margin
        against ``msg_id`` wraparound aliasing.

        **Atomicity: every row or none.** One ``BEGIN IMMEDIATE``
        transaction covers the dedup check and every insert; a failure
        partway through (a corrupt event, a constraint violation) rolls
        back everything this call has written so far, the same
        all-or-nothing guarantee :meth:`append` already gives a single row.
        A caller must never observe a batch half-stored.

        ``node_cursor`` is upserted once, exactly as :meth:`append` does for
        a single event: ``last_msg_id``/``last_node_seq`` are this frame's
        shared ``msg_id``/``node_seq``; ``last_seq`` is the store ``seq`` of
        the *last* row this call inserted, so a later cursor read still
        names a real, existing row.

        Raises ``ValueError`` for anything that is not one well-formed
        frame's worth of node-sourced events sharing one ``(node_id,
        msg_id)`` — the same defensive discipline :meth:`append` already
        applies to a missing ``msg_id``.
        """
        if not events:
            raise ValueError("append_batch requires at least one event")

        first = events[0]
        if first.source != "node" or first.msg_id is None:
            raise ValueError("append_batch is for node-sourced, msg_id-bearing events only")
        for event in events:
            if (event.source, event.node_id, event.msg_id) != (
                first.source,
                first.node_id,
                first.msg_id,
            ):
                raise ValueError(
                    "append_batch requires every event to share one (node_id, msg_id) "
                    "frame -- see this method's own docstring for why the dedup check "
                    "runs once per frame, not once per event"
                )

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            # `_hello_boot_count` is always `None` here: `first.msg_type` is
            # never HELLO in practice (`adapters/ingest.py` only ever routes
            # READS frames through this method -- see this method's own
            # docstring), and this call site never references it, so the
            # upsert below stays exactly as it was, byte for byte.
            node_seq, duplicate_of, _hello_boot_count = self._next_node_seq_and_dedup(conn, first)
            if duplicate_of is not None:
                conn.execute("COMMIT")
                return AppendedBatch(seqs=(), node_seq=None, duplicate_of=duplicate_of)

            seqs: list[int] = []
            observed_tags: set[bytes] = set()
            for event in events:
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
                seqs.append(seq)
                if event.tag is not None:
                    observed_tags.add(event.tag)

            # One statement per *distinct* tag, not per read: a feeding
            # frenzy's batch can repeat the same animal many times in one
            # frame (`append`'s own docstring on why this method exists at
            # all -- the same "once per frame, not once per row" discipline
            # applied here to the roster instead of to dedup). See
            # `append`'s own comment for why this sits inside the batch's
            # one BEGIN IMMEDIATE rather than as a separate write.
            for tag in observed_tags:
                conn.execute("INSERT OR IGNORE INTO specimen (tag) VALUES (?)", (tag,))

            assert node_seq is not None
            conn.execute(
                """
                INSERT INTO node_cursor (node_id, last_msg_id, last_seq, last_node_seq)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    last_msg_id   = excluded.last_msg_id,
                    last_seq      = excluded.last_seq,
                    last_node_seq = excluded.last_node_seq
                """,
                (first.node_id, first.msg_id, seqs[-1], node_seq),
            )

            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

        return AppendedBatch(seqs=tuple(seqs), node_seq=node_seq, duplicate_of=None)

    def specimens(self) -> list[SpecimenRow]:
        """The whole roster, ordered by tag -- station/specimen-roster: what
        the operator's roster page lists and what ``GET /api/specimens``
        returns.

        ``times_seen``/``last_seen_epoch`` are read from the
        ``specimen_activity`` view (``_SCHEMA``), never from a column on
        ``specimen`` itself -- see that view's own comment for why a stored
        counter here would be this project's characteristic defect.
        """
        rows = self._conn.execute(
            """
            SELECT s.tag, s.species, s.band_number, s.of_interest,
                   COALESCE(a.times_seen, 0), a.last_seen_epoch,
                   s.species_code, s.sex, s.age, s.registered
              FROM specimen s
              LEFT JOIN specimen_activity a ON a.tag = s.tag
             ORDER BY s.tag
            """
        ).fetchall()
        trap_ids: dict[bytes, list[int]] = {}
        for tag, node_id in self._conn.execute(
            "SELECT tag, node_id FROM specimen_trap_interest ORDER BY node_id"
        ):
            trap_ids.setdefault(tag, []).append(node_id)
        capture_days: dict[bytes, list[str]] = {}
        for tag, day in self._conn.execute(
            "SELECT tag, day FROM specimen_capture_day ORDER BY day"
        ):
            capture_days.setdefault(tag, []).append(day)
        return [
            SpecimenRow(
                tag=tag,
                species=species,
                band_number=band_number,
                of_interest=bool(of_interest),
                times_seen=times_seen,
                last_seen_epoch=last_seen_epoch,
                species_code=species_code,
                sex=sex,
                age=age,
                registered=bool(registered),
                trap_ids=tuple(trap_ids.get(tag, ())),
                capture_days=tuple(capture_days.get(tag, ())),
            )
            for (
                tag,
                species,
                band_number,
                of_interest,
                times_seen,
                last_seen_epoch,
                species_code,
                sex,
                age,
                registered,
            ) in rows
        ]

    def wanted_tags(self) -> list[bytes]:
        """The current shared target list (messages.md's open question 2,
        answered 2026-09-26: "shared by the whole network"): every specimen
        the study currently wants, tag order (deterministic, so two
        consecutive calls -- one per node about to be commanded -- can never
        disagree about what "the current list" means).
        """
        rows = self._conn.execute(
            "SELECT tag FROM specimen WHERE of_interest = 1 ORDER BY tag"
        ).fetchall()
        return [row[0] for row in rows]

    def wanted_tags_for(self, node_id: int) -> list[bytes]:
        """What this trap should hunt: subjects of interest in every trap, plus
        those marked for it alone. Tag order, so two calls cannot disagree."""
        rows = self._conn.execute(
            """
            SELECT tag FROM specimen WHERE of_interest = 1
            UNION
            SELECT tag FROM specimen_trap_interest WHERE node_id = ?
            ORDER BY tag
            """,
            (node_id,),
        ).fetchall()
        return [row[0] for row in rows]

    def set_interest(self, tag: bytes, *, all_traps: bool, node_ids: Sequence[int]) -> bool:
        """Replace where a subject is of interest. Marking one registers it."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = conn.execute(
                """
                UPDATE specimen SET of_interest = ?,
                       registered = CASE WHEN ? THEN 1 ELSE registered END
                 WHERE tag = ?
                """,
                (1 if all_traps else 0, 1 if all_traps or node_ids else 0, tag),
            )
            found = cursor.rowcount > 0
            if found:
                conn.execute("DELETE FROM specimen_trap_interest WHERE tag = ?", (tag,))
                if not all_traps:
                    conn.executemany(
                        "INSERT INTO specimen_trap_interest (tag, node_id) VALUES (?, ?)",
                        [(tag, node_id) for node_id in sorted(set(node_ids))],
                    )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return found

    def register_specimens(
        self,
        specimens: Mapping[bytes, Mapping[str, str]],
        capture_days: Mapping[bytes, Iterable[date]] | None = None,
    ) -> tuple[int, int]:
        """Create or update registered subjects, never touching who is of
        interest: re-importing the list must not change what a trap hunts.
        A subject's capture days are replaced by the ones given for it, so a
        corrected date in the list does not leave the wrong one behind.
        Returns how many were created and how many updated."""
        conn = self._conn
        created = 0
        updated = 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            for tag, fields in specimens.items():
                exists = conn.execute("SELECT 1 FROM specimen WHERE tag = ?", (tag,)).fetchone()
                if exists is None:
                    conn.execute("INSERT INTO specimen (tag, registered) VALUES (?, 1)", (tag,))
                    created += 1
                else:
                    conn.execute("UPDATE specimen SET registered = 1 WHERE tag = ?", (tag,))
                    updated += 1
                for key in _SPECIMEN_DESCRIBING_FIELDS:
                    if key in fields:
                        conn.execute(
                            f"UPDATE specimen SET {key} = ? WHERE tag = ?", (fields[key], tag)
                        )
            for tag, days in (capture_days or {}).items():
                if tag not in specimens:
                    continue
                conn.execute("DELETE FROM specimen_capture_day WHERE tag = ?", (tag,))
                conn.executemany(
                    "INSERT OR IGNORE INTO specimen_capture_day (tag, day) VALUES (?, ?)",
                    [(tag, day.isoformat()) for day in days],
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return created, updated

    def trap_profiles(self) -> dict[int, TrapProfile]:
        rows = self._conn.execute(
            "SELECT node_id, name, has_latch, setup_done FROM trap_profile"
        ).fetchall()
        return {
            node_id: TrapProfile(
                node_id=node_id,
                name=name,
                has_latch=None if has_latch is None else bool(has_latch),
                setup_done=bool(setup_done),
            )
            for node_id, name, has_latch, setup_done in rows
        }

    def update_trap_profile(self, node_id: int, fields: Mapping[str, object]) -> None:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("INSERT OR IGNORE INTO trap_profile (node_id) VALUES (?)", (node_id,))
            for key in ("name", "has_latch", "setup_done"):
                if key in fields:
                    conn.execute(
                        f"UPDATE trap_profile SET {key} = ? WHERE node_id = ?",
                        (fields[key], node_id),
                    )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def reads_between(
        self, *, since_epoch: float = 0.0, until_epoch: float | None = None
    ) -> list[StoredEvent]:
        """READS rows received in a window, oldest first, for visits and exports."""
        rows = self._conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS} FROM event
             WHERE msg_type = ? AND received_epoch >= ? AND received_epoch < ?
             ORDER BY seq ASC
            """,
            (
                payloads.READS,
                since_epoch,
                until_epoch if until_epoch is not None else float("inf"),
            ),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def log_events(
        self, msg_types: Sequence[int], *, limit: int, node_id: int | None = None
    ) -> list[StoredEvent]:
        """The newest events of these types, newest first."""
        marks = ",".join("?" for _ in msg_types)
        node_clause = "" if node_id is None else "AND node_id = ?"
        node_args = () if node_id is None else (node_id,)
        rows = self._conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS} FROM event
             WHERE msg_type IN ({marks}) {node_clause}
             ORDER BY seq DESC LIMIT ?
            """,
            (*msg_types, *node_args, limit),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def captures(self) -> list[CaptureRow]:
        """Every release, newest first, each with the result the operator gave."""
        rows: list[CaptureRow] = []
        unanswered: dict[int, int] = {}
        for event in self.log_events(
            (payloads.CAPTURED, OPERATOR_CAPTURE_ACKNOWLEDGED), limit=10_000
        )[::-1]:
            if event.msg_type == payloads.CAPTURED:
                unanswered[event.node_id] = len(rows)
                rows.append(
                    CaptureRow(
                        node_id=event.node_id,
                        tag=event.tag,
                        epoch=(
                            event.node_epoch
                            if event.node_epoch is not None
                            else event.received_epoch
                        ),
                        result=None,
                    )
                )
            elif event.node_id in unanswered:
                index = unanswered.pop(event.node_id)
                result = event.fields.get("result")
                rows[index] = replace(
                    rows[index], result=result if isinstance(result, str) else "capturado"
                )
        return rows[::-1]

    def update_specimen(self, tag: bytes, fields: Mapping[str, object]) -> bool:
        """Apply only the keys actually present in ``fields`` -- never a
        full overwrite -- to the specimen row for ``tag``.

        Returns ``False`` without writing anything when no such specimen
        exists: the roster grows only from an observed read or capture
        (pending.md, "nothing has to be pre-registered") -- never from this
        method, which never inserts a row, only updates one that is already
        there.
        """
        assignments: list[str] = []
        values: list[object] = []
        for key in _SPECIMEN_EDITABLE_FIELDS:
            if key not in fields:
                continue
            value = fields[key]
            if key in ("of_interest", "registered"):
                value = 1 if value else 0
            assignments.append(f"{key} = ?")
            values.append(value)

        # Describing or marking a subject is saying it is a real one.
        if "registered" not in fields and (
            fields.get("of_interest") or any(fields.get(key) for key in _SPECIMEN_DESCRIBING_FIELDS)
        ):
            assignments.append("registered = ?")
            values.append(1)

        if not assignments:
            row = self._conn.execute("SELECT 1 FROM specimen WHERE tag = ?", (tag,)).fetchone()
            return row is not None

        values.append(tag)
        cursor = self._conn.execute(
            f"UPDATE specimen SET {', '.join(assignments)} WHERE tag = ?", values
        )
        return cursor.rowcount > 0

    def _next_node_seq_and_dedup(
        self, conn: sqlite3.Connection, event: StationEvent
    ) -> tuple[int | None, int | None, int | None]:
        """DD6/DD8: bounded-window dedup, keyed on the station's own node_seq.

        Returns ``(node_seq, duplicate_of, hello_boot_count)``. ``node_seq``
        is ``None`` for anything not sourced from a node (design.md: "NULL
        when source <> 'node'") and also ``None`` when a duplicate is found,
        since a duplicate consumes no new ordinal. ``hello_boot_count`` is
        the value :meth:`append`'s own ``node_cursor`` upsert must persist as
        this node's ``boot_count`` high-water mark -- non-``None`` only for a
        ``HELLO`` event, so every other message type's upsert leaves that
        column exactly as it already was (that column does not exist on the
        wire for any other message type).

        **docs/pending.md, "A node goes invisible after every power cut":**
        a node's ``msg_id`` restarts at 1 on every boot, so a restarted
        node's first few messages routinely repeat ``msg_id``s this dedup
        window already holds from before the restart -- read as replays by
        the ordinary check below, forever, until the window ages them out on
        its own (up to ``DEDUP_WINDOW`` messages away). ``HELLO``
        (``docs/protocol/messages.md``) carries ``boot_count``, a counter a
        node increments once per boot, and is what lets the station tell
        "this is a restart" from "this is a replay" at all:

        * A ``HELLO`` whose ``boot_count`` is genuinely newer than the one
          on file (:func:`_boot_count_is_newer` -- wraparound-aware, never a
          plain ``>``) means this node has rebooted since the station last
          heard from it. ``previous_node_seq`` is widened by exactly
          ``DEDUP_WINDOW`` before this event's own ``node_seq`` is drawn from
          it: since ``node_seq`` only ever increases for a given node, every
          row this node has ever stored has ``node_seq`` no greater than the
          *old* ``previous_node_seq`` -- and the ordinary dedup check below
          keys on ``node_seq > previous_node_seq - DEDUP_WINDOW``, so adding
          a full ``DEDUP_WINDOW`` moves that floor strictly past every row
          this node has ever stored, unconditionally, regardless of how full
          the window happened to be. No stored ``node_cursor`` row at all (a
          node the station has never heard from) counts as "newer" too --
          there is no dedup state to reset, but nothing to lose by treating
          it as such either.
        * ``boot_count`` merely equal to what is on file (the expected
          retry -- the firmware resends ``HELLO`` until acknowledged) or
          *lower* (a stale retransmission arriving late, or a replaced
          board's counter starting over) must never reset anything: a lower
          value winning here would let old data wipe a live node's cursor
          and reopen the exact collision this exists to close.
        * ``HELLO`` itself never reaches the ordinary duplicate check below,
          unconditionally: its own ``msg_id`` is whatever the wire gives it
          (typically 1 -- the id most likely to collide with what the
          previous boot already stored), so running it through the same
          ``(node_id, msg_id)`` check every other message type uses would
          read a retried ``HELLO`` as a duplicate of itself and silently
          discard the very message that exists to reset the window in the
          first place. It always draws a fresh ``node_seq`` instead.
        """
        if event.source != "node":
            return None, None, None

        row = conn.execute(
            "SELECT last_node_seq, boot_count FROM node_cursor WHERE node_id = ?",
            (event.node_id,),
        ).fetchone()
        previous_node_seq = row[0] if row is not None else 0
        stored_boot_count = row[1] if row is not None else None

        if event.msg_type == payloads.HELLO:
            boot_count = event.fields.get("boot_count")
            # Defensive, like `_apply_heartbeat`'s own `isinstance` guards
            # (adapters/ingest.py) -- `decode_hello` always emits an int, but
            # this dataclass is JSON round-tripped through `fields` and
            # nothing here re-proves that on the way back out.
            if isinstance(boot_count, int) and (
                stored_boot_count is None or _boot_count_is_newer(boot_count, stored_boot_count)
            ):
                previous_node_seq += DEDUP_WINDOW
                hello_boot_count = boot_count
            else:
                # Not newer (or unreadable): the high-water mark on file is
                # left exactly as it was, never downgraded.
                hello_boot_count = stored_boot_count if stored_boot_count is not None else 0
            return previous_node_seq + 1, None, hello_boot_count

        duplicate = conn.execute(
            """
            SELECT seq FROM event
             WHERE node_id = ? AND msg_id = ? AND node_seq > ?
             ORDER BY node_seq DESC LIMIT 1
            """,
            (event.node_id, event.msg_id, previous_node_seq - DEDUP_WINDOW),
        ).fetchone()
        if duplicate is not None:
            return None, duplicate[0], None

        return previous_node_seq + 1, None, None

    def cursor(self, node_id: int) -> Cursor | None:
        row = self._conn.execute(
            "SELECT last_msg_id, last_seq, last_node_seq FROM node_cursor WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        return Cursor(last_msg_id=row[0], last_seq=row[1], last_node_seq=row[2])

    def record_reported_targets(
        self, node_id: int, *, targets_version: int, tags: Sequence[bytes]
    ) -> None:
        """Durable copy of a completed `TARGETS` reassembly (docs/protocol/
        messages.md) -- `node_cursor`'s own "latest known per-node fact"
        cache (SCHEMA_VERSION 4), the same shape `boot_count` already
        established there: derived from data already durable elsewhere
        (this reassembly's own chunk rows, each already appended through
        the ordinary `TARGETS` uplink path -- `Store.append`, called
        before this) but kept here too so a restart's rehydrate reads it
        back in one row, not by re-walking however many chunk events this
        node has ever sent over its whole deployment.

        Called only once a reassembly actually completes
        (`adapters/ingest.py::_apply_targets_chunk`, from `Registry.
        reported_targets_chunk`'s own return) -- never per chunk, unlike
        the `node_cursor` upsert :meth:`append` performs unconditionally
        for every node-sourced row regardless of type.

        A plain ``UPDATE``, never an upsert: by the time this runs, the
        raw chunk event that just completed the reassembly has already
        gone through :meth:`append` in this same call, which unconditionally
        upserts a `node_cursor` row for every node-sourced event -- so a row
        for ``node_id`` is always already there.

        Best-effort, like every other registry-derived write step 8 of the
        S9 ordering makes (`adapters/ingest.py`'s own module docstring): a
        crash between the chunk's own durable append and this call loses
        only this cache, never the chunk data it is derived from, and costs
        at most one redundant `GET_TARGETS` after the next restart.
        """
        self._conn.execute(
            "UPDATE node_cursor SET reported_targets_version = ?, reported_tags = ? "
            "WHERE node_id = ?",
            (targets_version, json.dumps([tag.hex() for tag in tags]), node_id),
        )

    def reported_targets(self, node_id: int) -> tuple[int, tuple[bytes, ...]] | None:
        """The trap's last fully-reassembled `TARGETS` reply, read back for
        :meth:`rehydrate`'s own restart path -- `node_cursor`'s own cache
        (SCHEMA_VERSION 4, :meth:`record_reported_targets`), never
        recomputed by replaying the raw chunk log at boot. ``None`` when
        no row exists at all, or when this node has never had a completed
        reassembly recorded (both columns ``NULL`` -- a pre-migration row,
        or one this station has simply never fetched targets for yet).
        """
        row = self._conn.execute(
            "SELECT reported_targets_version, reported_tags FROM node_cursor WHERE node_id = ?",
            (node_id,),
        ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            return None
        targets_version, tags_json = row
        tags = tuple(bytes.fromhex(tag) for tag in json.loads(tags_json))
        return targets_version, tags

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

    def recent_reads(self, *, since_epoch: float) -> list[StoredEvent]:
        """READS (0x06) rows received at or after ``since_epoch``, oldest
        first, across every node in the roster (station/live-reads-view).

        The raw material the operator's live-reads view groups by tag --
        client-side, at render time, under a "still here" gap the operator
        controls with a slider (design.md 3.2's amendment: that gap is a
        biological parameter, never computed or cached here). Unlike
        :meth:`events_since`, this is not scoped to one ``node_id`` — the
        view covers the whole roster at once; the HTTP layer (``app.py``)
        is what partitions this result by node.

        Oldest first, the same order :meth:`events_since` already returns,
        so a caller's own forward gap-walk (`is this read close enough in
        time to the previous one to join its group`) never has to reverse
        the list first.
        """
        rows = self._conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS} FROM event
             WHERE msg_type = ? AND received_epoch >= ?
             ORDER BY seq ASC
            """,
            (payloads.READS, since_epoch),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def rehydrate(self, registry: Registry) -> None:
        """Rebuild ``registry``'s per-node state from stored events.

        Four replay steps per node (CRITICAL 1, sdd-verify field-station-p4
        batch 2 — design.md section 6's old three-step order read as if
        ``state`` were exclusively the heartbeat's business; it is not:
        ``ingest.py``'s own live path sets ``state`` from a HEARTBEAT, a
        CAPTURED *or* a FAULT, whichever arrives, and rehydration must
        answer the same question the same way):

        (a) The **state race**: :meth:`_rehydrate_heartbeat`,
            :meth:`_rehydrate_capture` and :meth:`_rehydrate_fault` each
            return the node's latest row of their own type as a candidate
            fact, if readable. Whichever candidate has the highest store
            ``seq`` — the most recent report, of any type — wins and is
            applied via :meth:`Registry.restore_reported_state`. No
            candidate at all (every state-bearing row missing or
            unreadable) leaves ``state`` at its own honest ``None``
            (WARNING J) rather than defaulting to anything.
        (b) ``battery_mv``/``targets_version`` — restored from the latest
            HEARTBEAT regardless of whether it won (a): those fields exist
            on no other report type (:meth:`_rehydrate_heartbeat`, via
            :meth:`Registry.restore_heartbeat_fields`).
        (c) The unacknowledged capture object — :meth:`_rehydrate_capture`,
            via :meth:`Registry.restore_capture`, independent of which
            report won the state race (S3: still surfaced either way).
        (d) The *desired* command state — :meth:`_rehydrate_desired_command`,
            replayed only if (a)/(b) have not already satisfied it (DD15),
            so it must run last, after the node's final state is settled.
            ``desired_tags`` (S2) replays on its own regardless, the same
            independence :meth:`Registry.command_set_targets` gives it live.

        Reachability is deliberately never rehydrated — a restarted
        station has not heard from anyone yet (S11).

        Called from ``app.py``'s lifespan, before the HTTP server accepts
        (design.md section 6) — CRITICAL 1 (sdd-verify): this method had no
        production caller at all before that wiring existed; every test
        that exercised it fed an already-populated registry to
        ``create_app`` by hand instead.

        **Skip-and-count, never raise (CRITICAL 2; widened by CRITICAL A,
        sdd-verify field-station-p4 batch 1).** An unreadable row is
        skipped, logged with its node id, and counted in
        :attr:`rehydration_errors` instead of propagating. Granularity is
        per row-source, not per node (WARNING J's own question, decided
        deliberately here rather than inherited): a node with one
        unreadable row still shows whatever its *other* rows honestly
        support, the same partial-but-honest shape ``battery_mv``/
        ``last_seen`` already had — dropping the whole node over one bad
        row would throw away real, readable data for no gain.

        The guard is ``except Exception``, not a hand-picked tuple: a
        narrower ``except ValueError`` missed three exception types
        CRITICAL A found (``OverflowError``, two ``AttributeError``
        shapes), and predicting how corrupt data will fail cannot be done
        reliably. The contract here genuinely is indifference to the error
        type. ``BaseException`` stays uncaught. The roster query
        (:meth:`_known_node_ids`) and the loop over it are inside this
        guard too now, not only each node's replay steps — a
        ``sqlite3.DatabaseError`` reading ``node_cursor`` used to sit
        above the ``try`` and would have escaped regardless.

        A restart that cannot read one row must still be a restart —
        ``event-store/spec.md``'s "Registry rehydration at boot", and
        ``station-deployment/spec.md``'s S7.
        """
        self.rehydration_errors = 0
        self.roster_lost = False
        self.roster_nodes_expected = 0
        self.roster_nodes_rehydrated = 0
        try:
            node_ids = self._known_node_ids()
            self.roster_nodes_expected = self._count_all_known_nodes()
        except Exception:  # CRITICAL A, see docstring above -- deliberately broad.
            logger.exception("rehydrate: could not read the node roster")
            self.rehydration_errors += 1
            self.roster_lost = True  # WARNING K(b): degraded boot, not a bad-row count
            return
        self.roster_nodes_rehydrated = len(node_ids)
        for node_id in node_ids:
            facts: list[_ReportedFact] = []
            a_state_row_was_unreadable = False
            for state_step in (
                self._rehydrate_heartbeat,
                self._rehydrate_capture,
                self._rehydrate_fault,
            ):
                try:
                    fact = state_step(node_id, registry)
                except Exception:  # CRITICAL A, see docstring above -- deliberately broad.
                    logger.exception("rehydrate: skipping an unreadable row for node %s", node_id)
                    self.rehydration_errors += 1
                    a_state_row_was_unreadable = True
                    continue
                if fact is not None:
                    facts.append(fact)

            # CRITICAL 1: the winner is the most recent REPORT of any kind,
            # not "the heartbeat, always". WARNING J: no winner leaves
            # `state` at `None`, never a fabricated `IDLE`.
            #
            # WARNING A (sdd-verify field-station-p4 batch 2a): "most recent"
            # is deliberately store ARRIVAL (`seq`), never the node's own
            # `node_epoch` (persisted, never read here) -- pinned by
            # test_a_backlog_replayed_report_does_not_win_the_state_race_by_
            # the_nodes_own_older_clock. GET_BACKLOG replays what a node
            # buffered while the link was down, so a late-arriving stale
            # report is a designed-for case; trusting `node_epoch` would mean
            # trusting a clock `clock_trust.py` exists because the station
            # may not, pre-sync (S12) -- the same reasoning `DEDUP_WINDOW`'s
            # comment gives for count-bounded, not time-bounded, dedup.
            # Switching to `node_epoch` is a real option but a bigger one: it
            # would have to reckon with per-event `clock_trust`, not a
            # one-line key swap.
            #
            # Correction (verify-report-batch2.md, WARNING 5): `seq` is not
            # merely "arrival, not node_epoch" -- it is also the one key of
            # the two that survives a stepped station clock. `fact.at` is
            # `_epoch_to_datetime(received_epoch)`, and `received_epoch` is
            # this station's own `Clock.now()` (`system_now()`, clock.py) --
            # the same clock `clock_trust.py` exists because it is *unsynced
            # at boot* and can be stepped **backward** once chrony/timesyncd
            # actually syncs it. `seq`, an autoincrement primary key, cannot
            # regress; `received_epoch` can. `apply-progress-batch2a.md`
            # previously recorded `received_epoch` as "monotonic with seq in
            # every single-writer ingestion fixture" -- that claim is false,
            # and is corrected in place there. Pinned against a stepped-back
            # `received_epoch` fixture by
            # test_a_backlog_replayed_report_does_not_win_the_state_race_by_
            # the_nodes_own_older_clock.
            winner_msg_type: int | None = None
            if facts:
                winner = max(facts, key=lambda fact: fact.seq)
                registry.restore_reported_state(node_id, winner.state, at=winner.at)
                winner_msg_type = winner.msg_type
                if a_state_row_was_unreadable:
                    # WARNING C (verify-report-batch2a.md), WARNING J's own
                    # residual: an unread sibling row could have been newer
                    # than `winner`, silently superseding a real report with
                    # an older one. Say so rather than presenting a
                    # possibly-stale fact as current.
                    registry.mark_state_possibly_stale(node_id)

            try:
                self._rehydrate_reported_targets(node_id, registry)
            except Exception:  # CRITICAL A, see docstring above -- deliberately broad.
                logger.exception("rehydrate: skipping an unreadable row for node %s", node_id)
                self.rehydration_errors += 1

            try:
                self._rehydrate_desired_command(node_id, registry, winner_msg_type=winner_msg_type)
            except Exception:  # CRITICAL A, see docstring above -- deliberately broad.
                logger.exception("rehydrate: skipping an unreadable row for node %s", node_id)
                self.rehydration_errors += 1

    def _known_node_ids(self) -> list[int]:
        """CRITICAL 6 (sdd-verify field-station-p4 batch 2): `node_cursor`
        alone only ever holds node-sourced rows (:meth:`append`'s own
        ``if event.source == "node"`` guard) — a node the station has only
        ever *commanded* (an ARM, a DISARM, a ``SET_TARGETS``), never
        heard a frame from, had no row there and was silently dropped from
        every rehydration walk: an operator's command durably in ``event``,
        with nothing that ever read it back.

        Union in the distinct ``node_id``s of every operator-sourced row
        too, so a commanded-but-never-heard node is still visited. Kept
        narrower than the report's own simplest fix (``SELECT DISTINCT
        node_id FROM event``) specifically so this still queries
        ``node_cursor`` directly: that is what lets
        ``test_boot_survives_a_broken_node_roster_query`` (CRITICAL A) go
        on proving the roster query itself sits inside ``rehydrate()``'s
        guard, without also having to corrupt ``event``.

        **Widened again (WARNING 4, verify-report-batch2c.md): ``source =
        'station'`` rows are unioned in too.** ``adapters/clock.py``'s own
        ``STATION_TIME_SYNC_WITHHELD`` row names a ``node_id`` the station
        has neither heard from nor commanded -- a station-sourced-only
        node used to be invisible here while :meth:`_count_all_known_nodes`
        counted it anyway, a *by-design* gap (not corruption) that read as
        a permanently lost roster. Still deliberately narrower than
        ``SELECT DISTINCT node_id FROM event`` outright: that would also
        absorb ``source = 'node'`` rows, which is exactly the coverage
        ``test_boot_survives_a_broken_node_roster_query`` and
        ``test_a_partial_roster_says_so_instead_of_reading_healthy`` need
        ``node_cursor`` to answer *on its own*, uncorrupted by a wider net.
        """
        rows = self._conn.execute(
            """
            SELECT node_id FROM node_cursor
            UNION
            SELECT DISTINCT node_id FROM event WHERE source IN ('operator', 'station')
            ORDER BY node_id
            """
        ).fetchall()
        return [row[0] for row in rows]

    def _count_all_known_nodes(self) -> int:
        """WARNING 4 (verify-report-batch2.md): the source-agnostic ground
        truth :meth:`_known_node_ids` is supposed to match. Every row in
        ``event`` names a real ``node_id`` regardless of ``source`` or any
        JSON-field corruption elsewhere in the row (the column is a plain
        ``INTEGER NOT NULL``).

        **Correction (verify-report-batch2c.md, WARNING 4): the claim that
        a mismatch can only ever come from corruption was false.** Before
        that report, :meth:`_known_node_ids`'s union left out
        ``source = 'station'`` rows entirely, so a node known to this
        method only through a ``STATION_TIME_SYNC_WITHHELD`` row differed
        from :meth:`_known_node_ids` *by design*, permanently, with no
        corruption involved -- a gap this docstring wrongly called
        impossible. :meth:`_known_node_ids` now unions
        ``source = 'station'`` too, so the two are expected to agree unless
        a ``node_cursor`` row really was lost to a partial write, with the
        node's other rows in ``event`` intact. Run inside the same guard
        as the roster query itself: a whole-table failure here is exactly
        the roster-lost case already handled.
        """
        row = self._conn.execute("SELECT COUNT(DISTINCT node_id) FROM event").fetchone()
        return int(row[0])

    def _rehydrate_heartbeat(self, node_id: int, registry: Registry) -> _ReportedFact | None:
        """One row, one atomic attempt: the state candidate (step (a)) is
        computed first and may raise on an out-of-range byte
        (``trap_state_from_wire``) *or* a present-but-wrong-typed one
        (WARNING 1, verify-report-batch2.md — a corruption shape
        ``isinstance`` used to return ``None`` for silently, never raising,
        never counted, never flagged); ``battery_mv``/``targets_version``
        (step (b)) are validated the same way (BEHAVIOUR 3, verify-report-
        batch2d.md — these used to coerce silently to ``None`` instead of
        raising) before either is restored. Any one of the three failing
        fails this whole row — a corrupt sibling field drops the state
        candidate too, not only the field that was actually bad — the same
        way an unreadable ``state`` field already did before this batch —
        CRITICAL A's own per-row-source guard in ``rehydrate`` still counts
        it once either way, and a node with no *other* readable row for it
        stays out of the registry entirely, exactly as
        ``test_rehydrate_skips_and_counts_an_unreadable_row_without_
        blocking_other_nodes`` (CRITICAL 2) already pins.
        """
        heartbeat = self.latest(node_id, payloads.HEARTBEAT)
        if heartbeat is None:
            return None

        state_value = heartbeat.fields.get("state")
        if not isinstance(state_value, int):
            # WARNING 1: a row that cannot be read is a row that cannot be
            # read, whether the code expresses that by raising or by
            # returning `None` — this now raises into the same
            # `except Exception` guard `rehydrate()` already counts and
            # flags a fresher sibling row's failure through, instead of
            # answering "no fact" as if nothing were wrong.
            _unreadable("HEARTBEAT state", state_value)
        fact = _ReportedFact(
            seq=heartbeat.seq,
            state=trap_state_from_wire(state_value),
            at=_epoch_to_datetime(heartbeat.received_epoch),
            msg_type=payloads.HEARTBEAT,
        )

        battery_mv = heartbeat.fields.get("battery_mv")
        if not isinstance(battery_mv, int):
            # BEHAVIOUR 3 (verify-report-batch2d.md): `decode_heartbeat`
            # always emits `battery_mv` as an int from a fixed byte offset
            # -- a non-int here is the same corruption class `state` above
            # already raises for, not a legitimate absence.
            _unreadable("HEARTBEAT battery_mv", battery_mv)
        targets_version = heartbeat.fields.get("targets_version")
        if not isinstance(targets_version, int):
            # BEHAVIOUR 3: same correction, `targets_version` -- a field
            # `event-store/spec.md` names explicitly.
            _unreadable("HEARTBEAT targets_version", targets_version)
        registry.restore_heartbeat_fields(
            node_id, battery_mv=battery_mv, targets_version=targets_version
        )
        return fact

    def _rehydrate_capture(self, node_id: int, registry: Registry) -> _ReportedFact | None:
        """Step (c) — the capture object, gated on acknowledgement as
        before (CRITICAL 2) — and, since a CAPTURED row is itself a
        report exactly like a HEARTBEAT or FAULT (S4; ``ingest.py``'s own
        ``registry.captured()`` sets ``state`` too), step (a)'s CAPTURED
        candidate from ``state_after`` (WARNING 7 — decoded then
        discarded until this fix), *regardless* of acknowledgement:
        acknowledging a capture clears the capture object, never the
        reported state, matching ``Registry.acknowledge()``'s own scope.

        The capture object (above) is restored *before* ``state_after`` is
        checked, so a corrupt ``state_after`` (WARNING 1: raised below,
        rather than returned as a silent ``None``) still leaves S3's own
        capture-object guarantee intact — only the state-race candidate
        this row could have contributed is lost, exactly as a corrupt
        ``state_after`` already cost step (a) before this fix, never step
        (c).
        """
        captured = self.latest(node_id, payloads.CAPTURED)
        if captured is None:
            return None  # no CAPTURED row at all -- an honest "no fact", not corruption
        if captured.tag is None:
            # WARNING 1 (verify-report-batch2c.md, case D): `decode_captured`
            # always requires a tag, so a stored CAPTURED row with `tag`
            # NULL is a corruption shape the wire decoder could never
            # itself produce -- the same "not readable" as a wrong-typed
            # `state_after` below, not a legitimate absence.
            _unreadable("CAPTURED tag", captured.tag)

        acknowledged = self.latest(node_id, OPERATOR_CAPTURE_ACKNOWLEDGED)
        if acknowledged is None or acknowledged.seq <= captured.seq:
            registry.restore_capture(
                node_id=node_id,
                tag=captured.tag,
                at=_epoch_to_datetime(captured.received_epoch),
            )

        state_after = captured.fields.get("state_after")
        if not isinstance(state_after, int):
            # WARNING 1 (verify-report-batch2.md): same correction as
            # `_rehydrate_heartbeat` — a row that cannot be read must be
            # counted and flagged, not answered as "no fact" in silence.
            _unreadable("CAPTURED state_after", state_after)
        return _ReportedFact(
            seq=captured.seq,
            state=trap_state_from_wire(state_after),
            at=_epoch_to_datetime(captured.received_epoch),
            msg_type=payloads.CAPTURED,
        )

    def _rehydrate_fault(self, node_id: int, registry: Registry) -> _ReportedFact | None:
        """Step (a)'s FAULT candidate (CRITICAL 1) — not replayed at all
        before this fix. A FAULT payload carries no state byte of its
        own: the message type *is* the fact
        (``radio-gateway/spec.md``, "FAULT and silence handling").
        """
        fault = self.latest(node_id, payloads.FAULT)
        if fault is None:
            return None
        return _ReportedFact(
            seq=fault.seq,
            state=TrapState.FAULT,
            at=_epoch_to_datetime(fault.received_epoch),
            msg_type=payloads.FAULT,
        )

    def _rehydrate_reported_targets(self, node_id: int, registry: Registry) -> None:
        """The trap's last fully-reassembled `TARGETS` reply (docs/protocol/
        messages.md), restored from `node_cursor`'s own cache
        (`Store.reported_targets`, SCHEMA_VERSION 4) -- one row, not a
        replay of however many raw `TARGETS` chunk events this node has
        ever sent. Must run after the state-race loop above, whose
        `_rehydrate_heartbeat` step already restored `targets_version`
        inline (`registry.restore_heartbeat_fields`): `Registry.
        restore_reported_targets` compares against that value
        (`_note_targets_mismatch`) to decide whether a `GET_TARGETS` fetch
        should be armed again after this restart.

        Unreadable, corrupt JSON in `reported_tags` raises here -- caught
        by :meth:`rehydrate`'s own per-node guard, exactly like every
        other not-readable row this method's siblings already raise
        through `_unreadable`.

        A completed reply on file (``reported`` is not ``None``) always
        calls through, even for a node with no other trace in the registry
        yet -- a real, durable fact, the same "restoring desired_tags alone
        is enough to seed a trap" precedent `_rehydrate_desired_command`
        already sets. **Without one, this call is skipped outright unless
        some other step already created a `Trap` this same pass** (a real
        `HEARTBEAT`/`CAPTURED`/`FAULT`, restored earlier in this same
        per-node iteration): `Registry.restore_reported_targets(...,
        targets_version=None, tags=None)` would otherwise conjure a `Trap`
        into existence from nothing for a node this station genuinely knows
        nothing else about -- exactly the roster leak
        `test_an_acknowledge_only_node_is_counted_in_the_roster_and_
        rehydrates_to_nothing` (tests/test_cli.py) and this module's own
        WARNING-J-shaped "no fact, no fabricated entry" tests already pin.
        A trap that *does* already exist still needs its mismatch re-armed
        here (`_note_targets_mismatch`), the same check the live path's own
        `Registry.heartbeat` runs on every beat.
        """
        reported = self.reported_targets(node_id)
        if reported is not None:
            targets_version, tags = reported
            registry.restore_reported_targets(node_id, targets_version=targets_version, tags=tags)
            return

        try:
            registry.trap(node_id)
        except KeyError:
            return  # nothing else known about this node -- do not seed one
        registry.restore_reported_targets(node_id, targets_version=None, tags=None)

    _OPERATOR_COMMAND_MSG_TYPES: tuple[tuple[int, CommandKind], ...] = (
        (OPERATOR_ARM, CommandKind.ARM),
        (OPERATOR_DISARM, CommandKind.DISARM),
        (OPERATOR_SET_TARGETS, CommandKind.SET_TARGETS),
    )

    def _rehydrate_desired_command(
        self, node_id: int, registry: Registry, *, winner_msg_type: int | None
    ) -> None:
        """Step (d) — must run last, after the state race (a) and the
        heartbeat fields (b) have settled the node's final ``state``/
        ``targets_version``, since :func:`_reported_state` below reads
        both to decide whether a command is already satisfied (DD15).

        ``winner_msg_type`` (BEHAVIOUR 4 / SUGGESTION 5, verify-report-
        batch2d.md): which report won step (a)'s state race. The live
        path only ever runs ``satisfied_by`` from ``Registry.heartbeat``/
        ``captured`` — never ``fault`` (no state field of its own, S4).
        Before this fix, this method checked the *final* winning ``state``
        regardless of which report produced it, so a FAULT-won race
        silently satisfied a pending command the live path would have
        left untouched. Symmetric with :meth:`Registry.fault`'s own
        docstring.
        """
        latest_targets = self.latest(node_id, OPERATOR_SET_TARGETS)
        if latest_targets is not None:
            tags = tags_from_fields(latest_targets.fields)
            if tags is None:
                # BEHAVIOUR 1 (verify-report-batch2d.md): the same
                # `SET_TARGETS.tags` field is read at two sites -- this one
                # (the desired-tags list, independent of which command
                # wins below) and the winning command's own tags (case F,
                # further down). This site used to have no `else`: an
                # unreadable desired list silently rendered as "no
                # targets" while a later ARM/DISARM superseded the command
                # race, with nothing counted or flagged.
                _unreadable("SET_TARGETS tags", latest_targets.fields.get("tags"))
            registry.restore_desired_tags(node_id, tags)

        latest: StoredEvent | None = None
        latest_kind: CommandKind | None = None
        for msg_type, kind in self._OPERATOR_COMMAND_MSG_TYPES:
            row = self.latest(node_id, msg_type)
            if row is not None and (latest is None or row.seq > latest.seq):
                latest, latest_kind = row, kind
        if latest is None or latest_kind is None:
            return

        # docs/pending.md, "There is no way to cancel a pending command":
        # `OPERATOR_CANCEL_COMMAND` (`app.py`'s `/cancel` route,
        # `Registry.cancel_command`) is not one of
        # `_OPERATOR_COMMAND_MSG_TYPES`'s three kinds -- it is the
        # *absence* of one, so it is checked separately, after the race
        # above has already found whichever real command is newest. A
        # cancel newer than that command means an operator's `cancel_
        # command()` already cleared `pending_command` in memory when it
        # happened; without this check, a restart would silently reinstate
        # the older command from its own still-durable row, as though the
        # cancel had never happened -- the same "state that was true once,
        # presented as true now" defect this project keeps finding. A
        # cancel *older* than `latest` (an operator cancelled, then issued
        # a genuinely new command afterwards) changes nothing here: that
        # new command is what `latest` already names.
        cancel = self.latest(node_id, OPERATOR_CANCEL_COMMAND)
        if cancel is not None and cancel.seq > latest.seq:
            return

        targets_version: int | None = None
        if latest_kind is CommandKind.SET_TARGETS:
            version = latest.fields.get("targets_version")
            if not isinstance(version, int):
                # WARNING 1 (verify-report-batch2c.md, case E): a bare
                # `return` here used to drop the whole pending command
                # silently -- not counted, not logged -- while
                # `desired_tags` (restored independently, above) still
                # showed the edit as if it had been accepted.
                _unreadable("SET_TARGETS targets_version", version)
            targets_version = version

        reported = _reported_state(registry, node_id)
        if winner_msg_type == payloads.FAULT:
            # BEHAVIOUR 4 / SUGGESTION 5: a FAULT carries no state field of
            # its own (`domain/payloads.py::decode_fault` -- `code`/
            # `detail` only) -- treat this exactly like "nothing is known"
            # below, never like a report that can satisfy a command.
            reported = None
        # `reported is None` (see its own docstring): nothing is known about
        # this node yet, so there is no report to check the command against
        # -- it stays pending, the same as any other unconfirmed command.
        if reported is not None and Command(
            kind=latest_kind, targets_version=targets_version
        ).satisfied_by(reported):
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
            tags = tags_from_fields(latest.fields)
            if tags is None:
                # WARNING 1 (verify-report-batch2c.md, case F): `... or ()`
                # used to fabricate an *empty* desired list for unreadable
                # `tags` -- a trap configured to hunt specific tags
                # silently became a trap configured to hunt none, with
                # nothing saying the real list could not be read.
                _unreadable("SET_TARGETS tags", latest.fields.get("tags"))
            registry.command_set_targets(node_id, targets_version=targets_version, at=at, tags=tags)


def _reported_state(registry: Registry, node_id: int) -> ReportedState | None:
    """``None`` when nothing about ``node_id`` is known yet -- no row at
    all has ever named it a real report, whether because the registry has
    no entry (``KeyError``) or because it has one with ``state`` still at
    its own honest default (WARNING J).

    CRITICAL 1 (verify-report-batch2.md, discovered while parametrizing
    ``test_a_command_to_a_never_heard_from_node_survives_a_restart`` over
    ``POST /disarm``): the previous fallback, ``ReportedState(armed=False,
    ...)``, happened to keep a pending ``ARM`` correctly pending (any
    non-``ARMED`` reading does) but silently satisfied -- and dropped -- a
    pending ``DISARM`` on the very first rehydration of a node nobody has
    ever heard from. The node's own silence is not a report of "not
    armed"; S4's "reported, never assumed" applies exactly as much to "no
    report at all" as to a report that says something specific.
    :func:`_rehydrate_desired_command` must never ask ``satisfied_by``
    anything when this returns ``None`` -- an unconfirmed command stays
    pending until a real report confirms it, never until the station runs
    out of information about the node.
    """
    try:
        trap = registry.trap(node_id)
    except KeyError:
        return None
    if trap.state is None:
        return None
    return ReportedState(armed=trap.state is TrapState.ARMED, targets_version=trap.targets_version)


def _unreadable(what: str, value: object) -> NoReturn:
    """The single expression of "this value cannot be read" every not-
    readable guard in the rehydration walk raises through.

    **Record correction (Batch 2e, verify-report-batch2d.md Q7): the
    previous revision of this docstring said "five call sites" and named
    that as the total.** It was not: `rg "_unreadable" src/feeder_station/
    adapters/store.py` at the time found eight not-readable expressions in
    this module, and three of them -- `_rehydrate_heartbeat`'s
    `battery_mv`/`targets_version`, `_rehydrate_desired_command`'s
    desired-tags read -- still worked around a corrupt value silently
    instead of raising here (BEHAVIOUR 1/3, verify-report-batch2d.md). All
    eight now call this function, re-derived and re-counted for this fix,
    not carried over from the previous count. A hand-maintained call-site
    count is exactly what went stale across four consecutive verification
    rounds: re-run the `rg` above before trusting any number cited here,
    including this one, rather than treating this docstring as the source
    of truth.

    The rule this function exists to enforce, stated so it survives a
    stale count: **a value this station could not have produced on the
    wire is never represented the same way as a value it legitimately
    never received.** A missing row is an honest `None`; a row that
    exists but fails a type or range check is always a raise through here,
    never a second, quieter `None`-shaped path. Every caller sits inside
    :meth:`Store.rehydrate`'s own ``except Exception`` (CRITICAL A):
    raising, not returning early with a plausible default, is what makes
    the corruption counted in ``rehydration_errors`` and logged with a
    traceback instead of read back as an ordinary absence of data.
    """
    raise ValueError(f"{what} is not readable: {value!r}")
