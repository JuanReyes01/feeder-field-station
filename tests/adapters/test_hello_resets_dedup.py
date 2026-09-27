"""HELLO's `boot_count` resets a node's dedup state (docs/pending.md, "A node
goes invisible after every power cut").

`msg_id` restarts at 1 on every node boot, and `Store.append`'s dedup keys on
`(node_id, msg_id)` alone (`_next_node_seq_and_dedup`, `DEDUP_WINDOW`'s own
comment) -- with no notion of "this node restarted", a fresh 1, 2, 3... always
collides with whatever the previous boot already stored, and every frame
after a restart is silently read back as a replay.

`HELLO` (`docs/protocol/messages.md`) carries `boot_count`, a node-side
counter that increments once per boot and is exactly what breaks that tie.
Three rules, in the order the module docstring in `adapters/store.py`
states them:

1. `node_cursor` remembers the highest `boot_count` a node has ever reported
   (a schema migration, since live bench data already has rows without this
   column).
2. A `HELLO` whose `boot_count` is genuinely newer than the one on file
   resets that node's dedup window -- `_next_node_seq_and_dedup` widens the
   window's own floor by exactly `DEDUP_WINDOW`, so every previously stored
   `(node_id, msg_id)` pair for this node falls outside it. Never on
   `boot_count` merely equal (the expected retry) or lower (a stale
   retransmission, or a replaced board's counter starting over) -- either
   would either waste the window's margin for nothing, or let old data wipe
   a live node's cursor.
3. `HELLO` itself never goes through the ordinary dedup path -- it always
   gets a fresh `node_seq`, unconditionally. `HELLO`'s own `msg_id` is
   whatever the wire gives it (typically 1, the very id most likely to
   collide with what a previous boot already stored), and the firmware
   retries `HELLO` until acknowledged, so a retry landing as its own row
   (never silently discarded as a "duplicate" of itself) is the intended
   shape, not a leak.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from feeder_station.adapters.store import SCHEMA_VERSION, Store
from feeder_station.domain import payloads
from feeder_station.domain.events import StationEvent

TAG = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])


def _hello_event(*, node_id: int, msg_id: int, boot_count: int) -> StationEvent:
    """The same shape `domain/events.py::interpret` produces for a decoded
    `HELLO` frame (`domain/payloads.py::decode_hello`'s `Hello.boot_count`,
    surfaced through `_fields_of`'s `vars(decoded)`) -- built directly here,
    bypassing the frame/decoder layer, exactly as this file's sibling test
    modules already build node events for `Store` alone.
    """
    return StationEvent(
        source="node",
        node_id=node_id,
        msg_type=payloads.HELLO,
        msg_id=msg_id,
        node_epoch=None,
        tag=None,
        fields={"fw_version": (1, 0, 0), "app": 1, "targets_version": 0, "boot_count": boot_count},
        payload=b"",
    )


def _heartbeat_event(*, node_id: int, msg_id: int) -> StationEvent:
    return StationEvent(
        source="node",
        node_id=node_id,
        msg_type=payloads.HEARTBEAT,
        msg_id=msg_id,
        node_epoch=None,
        tag=None,
        fields={"state": 0, "battery_mv": 4000, "targets_version": 0},
        payload=b"",
    )


def _stored_boot_count(store: Store, node_id: int) -> int | None:
    row = store._conn.execute(
        "SELECT boot_count FROM node_cursor WHERE node_id = ?", (node_id,)
    ).fetchone()
    return None if row is None else row[0]


# --- The whole bug, end to end: a restart's colliding ids land after HELLO --


def test_a_hello_with_a_higher_boot_count_lets_colliding_ids_land_again(
    tmp_path: Path,
) -> None:
    """The bug this closes, reproduced in full: a boot's own `HELLO` plus
    three heartbeats, a restart, the *second* boot's `HELLO` reporting a
    higher `boot_count`, then three more heartbeats whose `msg_id`s exactly
    repeat the ones the first boot already stored -- `msg_id` is "per
    sender, never reused" (`docs/protocol/messages.md`) but restarts at 1
    on every boot, so the second boot's own `HELLO` reuses `msg_id` 1 and
    its heartbeats reuse 2, 3, 4, precisely as a real restarted node would
    send them. Without the reset, every one of the second boot's
    heartbeats is read back as a replay of the first boot's and none of
    them land -- `frames_in` climbs on the link, nothing new is ever
    written, and `/health` still says `ok: true`.
    """
    store = Store(tmp_path / "station.db")

    store.append(
        _hello_event(node_id=1, msg_id=1, boot_count=1), received_epoch=100.0, clock_trust="rtc"
    )
    for msg_id in (2, 3, 4):
        appended = store.append(
            _heartbeat_event(node_id=1, msg_id=msg_id), received_epoch=100.0 + msg_id,
            clock_trust="rtc",
        )
        assert appended.duplicate_of is None

    # -- restart: msg_id restarts at 1, boot_count increments --
    hello = store.append(
        _hello_event(node_id=1, msg_id=1, boot_count=2), received_epoch=200.0, clock_trust="rtc"
    )
    assert hello.duplicate_of is None  # HELLO itself always lands (rule 3)

    for msg_id in (2, 3, 4):
        appended = store.append(
            _heartbeat_event(node_id=1, msg_id=msg_id), received_epoch=300.0 + msg_id,
            clock_trust="rtc",
        )
        assert appended.duplicate_of is None, (
            f"msg_id {msg_id} was discarded as a replay after the restart -- the bug this "
            "closes is back"
        )

    # 1 HELLO + 3 heartbeats, twice over -- every one its own row.
    assert len(store.events_since(node_id=1, after_seq=0)) == 8


# --- Rule 1: node_cursor remembers boot_count, proven against existing data -


def _write_pre_boot_count_database(path: Path) -> None:
    """A ``station.db`` exactly as ``SCHEMA_VERSION`` 2 would have left it --
    ``node_cursor`` with no ``boot_count`` column, holding a real row, the
    same shape live bench data is in today. Written with raw ``sqlite3``,
    never through ``Store``, so this test does not depend on the very code
    path it exists to prove.
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
        CREATE TABLE specimen (
            tag         BLOB PRIMARY KEY,
            species     TEXT,
            band_number TEXT,
            of_interest INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '2')")
    conn.execute(
        "INSERT INTO event (node_seq, source, node_id, msg_type, msg_id, received_epoch, "
        "clock_trust, node_epoch, tag, fields, payload) VALUES "
        "(1, 'node', 1, ?, 9, 100.0, 'rtc', NULL, NULL, '{}', NULL)",
        (payloads.HEARTBEAT,),
    )
    conn.execute(
        "INSERT INTO node_cursor (node_id, last_msg_id, last_seq, last_node_seq) "
        "VALUES (1, 9, 1, 1)"
    )
    conn.commit()
    conn.close()


def test_migration_adds_boot_count_to_an_existing_node_cursor_row(tmp_path: Path) -> None:
    """A migration proven only on an empty database is not proven -- this
    one runs against a file a schema-version-2 station already wrote a real
    ``node_cursor`` row into, the same shape live bench data is in today.
    """
    db_path = tmp_path / "station.db"
    _write_pre_boot_count_database(db_path)

    store = Store(db_path)  # today's Store, opened against yesterday's file

    assert _stored_boot_count(store, node_id=1) == 0  # DEFAULT for a pre-existing row
    cursor = store.cursor(1)
    assert cursor is not None
    assert cursor.last_msg_id == 9  # untouched by the migration

    version = store._conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()[0]
    assert int(version) == SCHEMA_VERSION


def test_reopening_a_migrated_database_does_not_re_run_the_migration(tmp_path: Path) -> None:
    """Once ``schema_version`` reaches 3, ``ALTER TABLE ... ADD COLUMN`` must
    not run again -- a second attempt to add a column that already exists
    raises ``sqlite3.OperationalError``, which would otherwise crash every
    boot after the first migrated one.
    """
    db_path = tmp_path / "station.db"
    _write_pre_boot_count_database(db_path)

    Store(db_path).close()
    reopened = Store(db_path)  # must not raise

    assert _stored_boot_count(reopened, node_id=1) == 0


# --- Rule 2: only a strictly higher boot_count resets the dedup window -----


def test_an_equal_boot_count_does_not_reset_the_dedup_window(tmp_path: Path) -> None:
    """The expected retry: firmware resends `HELLO` until acknowledged, so
    the same `boot_count` arriving twice must never re-widen the window --
    only a genuinely higher `boot_count` may.
    """
    store = Store(tmp_path / "station.db")
    store.append(
        _hello_event(node_id=1, msg_id=1, boot_count=1), received_epoch=100.0, clock_trust="rtc"
    )
    store.append(
        _heartbeat_event(node_id=1, msg_id=7), received_epoch=101.0, clock_trust="rtc"
    )

    # A second HELLO, same boot_count -- the retry.
    store.append(
        _hello_event(node_id=1, msg_id=1, boot_count=1), received_epoch=102.0, clock_trust="rtc"
    )

    # The earlier heartbeat's own msg_id must still be recognised as a
    # duplicate -- if the retry had reset the window a second time, this
    # would incorrectly land as new.
    repeat = store.append(
        _heartbeat_event(node_id=1, msg_id=7), received_epoch=103.0, clock_trust="rtc"
    )
    assert repeat.duplicate_of is not None


def test_a_lower_boot_count_does_not_reset_the_dedup_window(tmp_path: Path) -> None:
    """Kill: a mutant that lets a *lower* `boot_count` reset dedup too (a
    naive `!=` instead of a real "newer than" check) would let a stale,
    late-arriving retransmission from a previous boot -- or a replaced
    board whose counter restarts from 0 -- wipe a live node's cursor and
    reopen it to the exact replay-collision bug this change closes.
    """
    store = Store(tmp_path / "station.db")
    store.append(
        _hello_event(node_id=1, msg_id=1, boot_count=5), received_epoch=100.0, clock_trust="rtc"
    )
    store.append(
        _heartbeat_event(node_id=1, msg_id=7), received_epoch=101.0, clock_trust="rtc"
    )

    # A stale HELLO, boot_count lower than what is already on file.
    store.append(
        _hello_event(node_id=1, msg_id=1, boot_count=3), received_epoch=102.0, clock_trust="rtc"
    )

    # The live node's own msg_id=7 must still be recognised as a duplicate --
    # a reset here would incorrectly let it land as new.
    repeat = store.append(
        _heartbeat_event(node_id=1, msg_id=7), received_epoch=103.0, clock_trust="rtc"
    )
    assert repeat.duplicate_of is not None
    assert _stored_boot_count(store, node_id=1) == 5  # the high-water mark, not downgraded


def test_boot_count_wraps_at_65535_and_still_resets(tmp_path: Path) -> None:
    """The wire field is two bytes (`docs/protocol/messages.md`): a genuine
    reboot that rolls the counter from 65535 back to 0 is a real "newer"
    transition, not a "lower" one -- a naive `>` would misread it as the
    latter and silently refuse to reset, reopening the same collision the
    very first restart the counter ever takes would hit.
    """
    store = Store(tmp_path / "station.db")
    store.append(
        _hello_event(node_id=1, msg_id=1, boot_count=65535), received_epoch=100.0, clock_trust="rtc"
    )
    store.append(
        _heartbeat_event(node_id=1, msg_id=2), received_epoch=101.0, clock_trust="rtc"
    )

    # -- restart: msg_id restarts at 1, boot_count wraps 65535 -> 0 --
    store.append(
        _hello_event(node_id=1, msg_id=1, boot_count=0), received_epoch=200.0, clock_trust="rtc"
    )

    repeat = store.append(
        _heartbeat_event(node_id=1, msg_id=2), received_epoch=201.0, clock_trust="rtc"
    )
    assert repeat.duplicate_of is None  # the reset took effect across the wrap
    assert _stored_boot_count(store, node_id=1) == 0


def test_a_hello_from_a_node_never_heard_from_before_is_accepted(tmp_path: Path) -> None:
    """No stored `node_cursor` row at all -- there is no dedup state to
    reset, but the first `HELLO` must still land and seed one.
    """
    store = Store(tmp_path / "station.db")

    hello = store.append(
        _hello_event(node_id=9, msg_id=1, boot_count=1), received_epoch=100.0, clock_trust="rtc"
    )

    assert hello.duplicate_of is None
    assert _stored_boot_count(store, node_id=9) == 1


# --- Rule 3: HELLO itself never goes through the ordinary dedup path -------


def test_a_retried_hello_is_never_discarded_as_a_duplicate_of_itself(tmp_path: Path) -> None:
    """Kill: routing `HELLO` through the ordinary `(node_id, msg_id)` dedup
    check (the path every other message type uses) would read the second,
    identical retry as a duplicate of the first and silently drop it --
    exactly the failure mode that would leave a restarted node's dedup
    state stuck reset-less, since the retry is indistinguishable from the
    first `HELLO` under that check (same `node_id`, same `msg_id`, well
    within `DEDUP_WINDOW`).
    """
    store = Store(tmp_path / "station.db")

    first = store.append(
        _hello_event(node_id=1, msg_id=1, boot_count=1), received_epoch=100.0, clock_trust="rtc"
    )
    second = store.append(
        _hello_event(node_id=1, msg_id=1, boot_count=1), received_epoch=100.5, clock_trust="rtc"
    )

    assert first.duplicate_of is None
    assert second.duplicate_of is None  # not read back as a duplicate of `first`
    assert first.seq != second.seq
    assert len(store.events_since(node_id=1, after_seq=0)) == 2


def test_a_hello_interleaved_with_heartbeats_leaves_heartbeat_dedup_intact(
    tmp_path: Path,
) -> None:
    """A `HELLO` arriving between two ordinary messages from the same node
    must not disturb the ordinary dedup path for those messages -- only its
    own `boot_count` transition governs a reset.
    """
    store = Store(tmp_path / "station.db")

    store.append(
        _hello_event(node_id=1, msg_id=1, boot_count=1), received_epoch=100.0, clock_trust="rtc"
    )
    store.append(
        _heartbeat_event(node_id=1, msg_id=7), received_epoch=101.0, clock_trust="rtc"
    )
    # A second HELLO, same boot_count -- no reset, so msg_id=7 is still a
    # live duplicate of the heartbeat above.
    store.append(
        _hello_event(node_id=1, msg_id=1, boot_count=1), received_epoch=102.0, clock_trust="rtc"
    )
    repeat = store.append(
        _heartbeat_event(node_id=1, msg_id=7), received_epoch=103.0, clock_trust="rtc"
    )
    assert repeat.duplicate_of is not None

    new = store.append(
        _heartbeat_event(node_id=1, msg_id=8), received_epoch=104.0, clock_trust="rtc"
    )
    assert new.duplicate_of is None
