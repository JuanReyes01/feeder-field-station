"""The event store: WAL, `synchronous=FULL`, dedup, cursors, rehydration.

Every scenario here traces to `openspec/changes/field-station-p4/specs/
event-store/spec.md`. The crash-consistency proxy at the bottom is named
explicitly as a proxy, not a hardware gate: see its own docstring.
"""

from __future__ import annotations

import random
import sqlite3
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

from feeder_station.adapters.store import Store
from feeder_station.domain import payloads
from feeder_station.domain.events import OPERATOR_CAPTURE_ACKNOWLEDGED, StationEvent
from feeder_station.domain.traps import Registry, TrapState

TAG = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])


def _node_event(
    *, node_id: int, msg_id: int, msg_type: int = payloads.HEARTBEAT, tag: bytes | None = None
) -> StationEvent:
    return StationEvent(
        source="node",
        node_id=node_id,
        msg_type=msg_type,
        msg_id=msg_id,
        node_epoch=None,
        tag=tag,
        fields={},
        payload=b"",
    )


# --- 4.5/4.6: the PRAGMA set --------------------------------------------------


def test_every_opened_connection_has_the_full_pragma_set(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")
    conn = store._conn

    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_size_limit").fetchone()[0] == 4194304
    assert conn.execute("PRAGMA trusted_schema").fetchone()[0] == 0


def test_a_freshly_reopened_connection_also_has_the_full_pragma_set(tmp_path: Path) -> None:
    """Not just the connection that created the file — every connection."""
    db_path = tmp_path / "station.db"
    Store(db_path).close()

    reopened = Store(db_path)
    conn = reopened._conn

    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


# --- 12.0b: one connection per thread, not one shared across all of them ------


def test_a_second_thread_gets_its_own_connection_not_the_first_threads(tmp_path: Path) -> None:
    """design.md section 7's own concurrency table: "SQLite connections |
    One per thread, created lazily... `check_same_thread` exists for a
    reason." Before slice 12, exactly one thread (whichever constructed
    the `Store`) ever called into it. Slice 12 is the first to hand the
    same `Store` to more than one thread (the gateway thread, the
    scheduler thread, and the existing HTTP threadpool) -- this is the
    regression proof: a second thread must be able to `append()` at all,
    which sqlite3's own `check_same_thread` default would otherwise refuse
    outright for a naively-shared single connection.
    """
    store = Store(tmp_path / "station.db")
    store.append(_node_event(node_id=1, msg_id=1), received_epoch=1.0, clock_trust="unset")

    errors: list[BaseException] = []

    def _append_from_another_thread() -> None:
        try:
            store.append(_node_event(node_id=2, msg_id=1), received_epoch=2.0, clock_trust="unset")
        except BaseException as exc:  # noqa: BLE001 -- the test asserts on this directly
            errors.append(exc)

    thread = threading.Thread(target=_append_from_another_thread)
    thread.start()
    thread.join(timeout=5.0)

    assert not errors, errors
    assert store.cursor(1) is not None
    assert store.cursor(2) is not None


# --- 4.7/4.8: write and fsync precede acknowledgement -------------------------


def test_write_and_fsync_precede_acknowledgement(tmp_path: Path) -> None:
    """`append()` is a plain blocking call: by the time it returns, the row
    is committed and visible. There is no queue and no background thread a
    caller could be handed a "go ahead and ACK" signal from any earlier.
    """
    store = Store(tmp_path / "station.db")
    event = _node_event(node_id=1, msg_id=1)

    appended = store.append(event, received_epoch=1.0, clock_trust="unset")

    assert appended.duplicate_of is None
    assert store.latest(1, payloads.HEARTBEAT) is not None


# --- 4.9/4.10: msg_id wraps without dropping or duplicating (D5) -------------


def test_msg_id_wraps_without_dropping_or_duplicating(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")

    for msg_id in (0xFFFE, 0xFFFF, 0x0000, 0x0001):
        appended = store.append(
            _node_event(node_id=1, msg_id=msg_id), received_epoch=float(msg_id), clock_trust="unset"
        )
        assert appended.duplicate_of is None

    stored = store.events_since(node_id=1, after_seq=0)
    assert [e.msg_id for e in stored] == [0xFFFE, 0xFFFF, 0x0000, 0x0001]


# --- 4.11/4.12: repeat within the window is deduplicated (DD8) ---------------


def test_repeat_within_the_window_is_deduplicated(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")
    event = _node_event(node_id=1, msg_id=5)

    first = store.append(event, received_epoch=1.0, clock_trust="unset")
    second = store.append(event, received_epoch=2.0, clock_trust="unset")

    assert first.duplicate_of is None
    assert second.duplicate_of == first.seq
    assert len(store.events_since(node_id=1, after_seq=0)) == 1


# --- 4.13/4.14: unacknowledged capture survives a restart --------------------


def test_unacknowledged_capture_survives_a_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "station.db"
    store = Store(db_path)
    capture = _node_event(node_id=7, msg_id=1, msg_type=payloads.CAPTURED, tag=TAG)
    store.append(capture, received_epoch=1.0, clock_trust="unset")
    store.close()

    reopened = Store(db_path)
    latest = reopened.latest(node_id=7, msg_type=payloads.CAPTURED)

    assert latest is not None
    assert latest.tag == TAG


# --- 4.15/4.16: cursor query returns events oldest-first ---------------------


def test_cursor_query_returns_events_oldest_first(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")
    for msg_id in (1, 2, 3):
        store.append(
            _node_event(node_id=1, msg_id=msg_id), received_epoch=float(msg_id), clock_trust="unset"
        )

    everything = store.events_since(node_id=1, after_seq=0)
    after_first = store.events_since(node_id=1, after_seq=everything[0].seq)

    assert [e.seq for e in after_first] == [everything[1].seq, everything[2].seq]


# --- 4.17/4.18: registry reflects last known state after restart ------------


def test_registry_reflects_last_known_state_after_restart(tmp_path: Path) -> None:
    """`Store.rehydrate()`'s own mechanics, called directly -- the "at
    startup" half of this scenario (that production ever calls
    `rehydrate()` at all, CRITICAL 1) is proven through the real boot path
    in `tests/test_cli.py::test_rehydration_runs_through_the_real_boot_path`
    instead; this test predates that one and calling `rehydrate()` itself
    is exactly what sdd-verify flagged it for, hence the split.
    """
    db_path = tmp_path / "station.db"
    store = Store(db_path)

    heartbeat = StationEvent(
        source="node",
        node_id=7,
        msg_type=payloads.HEARTBEAT,
        msg_id=1,
        node_epoch=None,
        tag=None,
        fields={"state": 1, "battery_mv": 3900, "targets_version": 2},  # 1 -> ARMED
        payload=b"",
    )
    store.append(heartbeat, received_epoch=1000.0, clock_trust="unset")

    capture = StationEvent(
        source="node",
        node_id=7,
        msg_type=payloads.CAPTURED,
        msg_id=2,
        node_epoch=None,
        tag=TAG,
        fields={"state_after": 2},
        payload=b"",
    )
    store.append(capture, received_epoch=1001.0, clock_trust="unset")
    store.close()

    reopened = Store(db_path)
    registry = Registry(silence_after=timedelta(minutes=15))
    reopened.rehydrate(registry)

    trap = registry.trap(7)
    assert trap.battery_mv == 3900
    assert trap.targets_version == 2
    # CRITICAL 2 (sdd-verify, field-station-p4): `state` is the heartbeat
    # replay's fact alone, never the capture's, however much more recent
    # the capture is -- trap-commands/spec.md, "The trap's displayed state
    # MUST always be whatever its own most recent HEARTBEAT or HELLO
    # reported." Before this fix, `_rehydrate_capture` called
    # `Registry.captured()`, which also stamps `state`/`last_seen` -- this
    # assertion used to read `TrapState.FIRED` "because the capture is the
    # more recent fact", which is exactly the regression: a capture replay
    # must never regress a state a later heartbeat already reported.
    assert trap.state is TrapState.ARMED
    assert trap.capture is not None
    assert trap.capture.tag == TAG
    assert trap.capture.acknowledged is False


def test_rehydrate_reflects_a_heartbeat_when_no_capture_exists(tmp_path: Path) -> None:
    db_path = tmp_path / "station.db"
    store = Store(db_path)
    heartbeat = StationEvent(
        source="node",
        node_id=9,
        msg_type=payloads.HEARTBEAT,
        msg_id=1,
        node_epoch=None,
        tag=None,
        fields={"state": 0, "battery_mv": 4100, "targets_version": 1},  # 0 -> IDLE
        payload=b"",
    )
    store.append(heartbeat, received_epoch=500.0, clock_trust="unset")
    store.close()

    reopened = Store(db_path)
    registry = Registry(silence_after=timedelta(minutes=15))
    reopened.rehydrate(registry)

    trap = registry.trap(9)
    assert trap.state is TrapState.IDLE
    assert trap.capture is None


def test_rehydrate_does_not_resurface_an_already_acknowledged_capture(tmp_path: Path) -> None:
    db_path = tmp_path / "station.db"
    store = Store(db_path)
    capture = StationEvent(
        source="node",
        node_id=4,
        msg_type=payloads.CAPTURED,
        msg_id=1,
        node_epoch=None,
        tag=TAG,
        fields={"state_after": 2},
        payload=b"",
    )
    store.append(capture, received_epoch=1.0, clock_trust="unset")
    acknowledgement = StationEvent(
        source="operator",
        node_id=4,
        msg_type=OPERATOR_CAPTURE_ACKNOWLEDGED,
        msg_id=None,
        node_epoch=None,
        tag=None,
        fields={},
        payload=None,
    )
    store.append(acknowledgement, received_epoch=2.0, clock_trust="unset")
    store.close()

    reopened = Store(db_path)
    registry = Registry(silence_after=timedelta(minutes=15))
    reopened.rehydrate(registry)

    # Node 4 never sent a heartbeat and its only capture is already
    # acknowledged: nothing reported anything left to rehydrate, so no trap
    # exists at all (S4 — state comes only from what was reported).
    assert 4 not in {trap.node_id for trap in registry.traps()}


# --- CRITICAL 2 (sdd-verify, field-station-p4): rehydrate is total ----------


def test_rehydrate_skips_and_counts_an_unreadable_row_without_blocking_other_nodes(
    tmp_path: Path,
) -> None:
    """The rule, not the example: an unreadable row must be skipped and
    counted (not merely "not crash the whole boot") *and* it must not cost
    any other node its own, perfectly readable state. `state=7` is written
    directly through `Store.append`, bypassing `payloads.decode_heartbeat`'s
    now-fixed decode boundary (task 1.2) — the row shape a pre-1.2 bit-flip,
    or any future row the boundary did not anticipate, would still produce.
    """
    db_path = tmp_path / "station.db"
    store = Store(db_path)
    corrupt = StationEvent(
        source="node",
        node_id=7,
        msg_type=payloads.HEARTBEAT,
        msg_id=1,
        node_epoch=None,
        tag=None,
        fields={"state": 7, "battery_mv": 4000, "targets_version": 1},  # 7: no known TrapState
        payload=b"",
    )
    store.append(corrupt, received_epoch=1000.0, clock_trust="unset")
    healthy = StationEvent(
        source="node",
        node_id=9,
        msg_type=payloads.HEARTBEAT,
        msg_id=1,
        node_epoch=None,
        tag=None,
        fields={"state": 1, "battery_mv": 4100, "targets_version": 1},  # 1 -> ARMED
        payload=b"",
    )
    store.append(healthy, received_epoch=1001.0, clock_trust="unset")
    store.close()

    reopened = Store(db_path)
    registry = Registry(silence_after=timedelta(minutes=15))

    reopened.rehydrate(registry)  # must not raise

    assert reopened.rehydration_errors == 1
    assert 7 not in {trap.node_id for trap in registry.traps()}  # the unreadable row, skipped
    assert registry.trap(9).state is TrapState.ARMED  # another node's row, unaffected


# --- 4.19/4.20: the kill -9 crash-consistency proxy --------------------------


def test_kill_minus_9_crash_proxy(tmp_path: Path) -> None:
    """A CI proxy for S9, not the hardware gate itself.

    This proves OUR code's crash-consistency: that `Store.append`'s
    `BEGIN IMMEDIATE` plus WAL plus `synchronous=FULL` leaves a recoverable
    file no matter when the process is killed mid-write, and that every
    event this harness saw "acknowledged" (printed by the writer subprocess
    right after `append()` returned) is actually present afterwards. It does
    NOT prove the SD card's controller honours fsync — that needs a physical
    power-pull rig that does not exist yet (design.md, "What cannot be
    verified without hardware"). A green run here is evidence about this
    code, not a claim about the storage medium.
    """
    db_path = tmp_path / "station.db"
    writer = Path(__file__).parent / "_crash_writer.py"
    total_events = 40

    for _trial in range(3):
        db_path.unlink(missing_ok=True)
        Path(f"{db_path}-wal").unlink(missing_ok=True)
        Path(f"{db_path}-shm").unlink(missing_ok=True)

        process = subprocess.Popen(
            [sys.executable, str(writer), str(db_path), str(total_events)],
            stdout=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None

        kill_after = random.randint(1, total_events - 1)
        acknowledged: set[int] = set()
        for _ in range(kill_after):
            line = process.stdout.readline()
            if not line:
                break
            acknowledged.add(int(line.strip()))

        process.kill()  # SIGKILL
        process.wait(timeout=5)

        conn = sqlite3.connect(db_path)
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"

        stored_msg_ids = {
            row[0] for row in conn.execute("SELECT msg_id FROM event WHERE node_id = 1")
        }
        conn.close()

        assert acknowledged <= stored_msg_ids
