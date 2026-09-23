"""The event store: WAL, `synchronous=FULL`, dedup, cursors, rehydration.

Every scenario here traces to `openspec/changes/field-station-p4/specs/
event-store/spec.md`. The crash-consistency proxy at the bottom is named
explicitly as a proxy, not a hardware gate: see its own docstring.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from feeder_station.adapters.ingest import _apply_to_registry
from feeder_station.adapters.store import Store, trap_state_from_wire
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
    # CRITICAL 1 (sdd-verify, field-station-p4 batch 2 — verify-report-2.md
    # Part 1.2, ordering B): `state` is whichever of HEARTBEAT/CAPTURED/
    # FAULT is the node's most recent REPORT by store `seq`, never "the
    # heartbeat, always" -- trap-commands/spec.md's S4 ("displayed state
    # MUST always be whatever its own most recent report said") makes no
    # exception for report type. Here the CAPTURED row (seq 2) is newer
    # than the HEARTBEAT (seq 1), and its own `state_after` field is the
    # source (WARNING 7), so the answer is FIRED.
    #
    # This assertion previously read `TrapState.ARMED` -- edited from
    # `FIRED` by an earlier fix pass that made `_rehydrate_capture` never
    # touch `state` at all, correct only for the mirror ordering (capture
    # older than the heartbeat) and, per verify-report-2.md, "exactly what
    # hid ordering B". Not simply flipped back: `test_registry_prefers_a_
    # newer_heartbeat_over_an_older_capture` below pins that mirror
    # ordering explicitly in the same file, so the two directions cannot
    # trade places silently again (task 2.4, option (b): a narrow
    # store-level guard alongside `test_cli.py`'s boot-level property
    # test, not a replacement for it -- this exercises `Store.rehydrate()`
    # directly, a layer the property test does not reach on its own).
    assert trap.state is TrapState.FIRED
    assert trap.capture is not None
    assert trap.capture.tag == TAG
    assert trap.capture.acknowledged is False


def test_registry_prefers_a_newer_heartbeat_over_an_older_capture(tmp_path: Path) -> None:
    """CRITICAL 1's other direction, in the same file as the test above
    (task 2.4, option (b)): a `CAPTURED` row older than the latest
    `HEARTBEAT` must not win the state race, mirroring `test_cli.py::
    test_an_older_capture_does_not_regress_a_newer_heartbeats_state`'s
    boot-level proof at the `Store.rehydrate()` unit level.
    """
    db_path = tmp_path / "station.db"
    store = Store(db_path)

    capture = StationEvent(
        source="node",
        node_id=7,
        msg_type=payloads.CAPTURED,
        msg_id=1,
        node_epoch=None,
        tag=TAG,
        fields={"state_after": 2},  # 2 -> FIRED, but older than the heartbeat below
        payload=b"",
    )
    store.append(capture, received_epoch=1000.0, clock_trust="unset")

    heartbeat = StationEvent(
        source="node",
        node_id=7,
        msg_type=payloads.HEARTBEAT,
        msg_id=2,
        node_epoch=None,
        tag=None,
        fields={"state": 0, "battery_mv": 4000, "targets_version": 1},  # 0 -> IDLE
        payload=b"",
    )
    store.append(heartbeat, received_epoch=1001.0, clock_trust="unset")
    store.close()

    reopened = Store(db_path)
    registry = Registry(silence_after=timedelta(minutes=15))
    reopened.rehydrate(registry)

    trap = registry.trap(7)
    assert trap.state is TrapState.IDLE  # the newer heartbeat wins, not the older capture
    assert trap.capture is not None  # S3: still surfaced regardless of which fact won the race
    assert trap.capture.tag == TAG


def test_a_winning_captured_row_reads_its_own_state_after_not_a_hardcoded_fired(
    tmp_path: Path,
) -> None:
    """WARNING 7 / task 2.2: `state_after` is decoded and now load-bearing
    for a winning CAPTURED row, not assumed to always be `FIRED`. Every
    other test in this file happens to use `state_after=2` (FIRED), which
    would let a hardcoded `TrapState.FIRED` pass unnoticed -- this one
    uses a different value specifically so a mutation that hardcodes the
    answer instead of reading the field is caught (verified by hand: a
    `state=TrapState.FIRED` hardcode in `_rehydrate_capture` survives
    every other test in this suite and is killed only by this one).

    CRITICAL A (verify-report-batch2a.md): **before that fix, this test
    asserted only the rehydrated half** (`ARMED`) -- correct alone, but
    never checked against the live path, whose `Registry.captured()`
    hardcoded `FIRED` for this exact log; the test pinned one side of a
    divergence it never looked at. **After: both paths are asserted and
    both must read `ARMED`** -- CRITICAL 1's own rule (rehydrated equals
    live), extended from the ordering axis to CRITICAL A's content axis.
    """
    db_path = tmp_path / "station.db"
    store = Store(db_path)
    capture = StationEvent(
        source="node",
        node_id=7,
        msg_type=payloads.CAPTURED,
        msg_id=1,
        node_epoch=None,
        tag=TAG,
        fields={"state_after": 1},  # 1 -> ARMED, not FIRED
        payload=b"",
    )
    store.append(capture, received_epoch=1000.0, clock_trust="unset")
    store.close()

    reopened = Store(db_path)
    registry = Registry(silence_after=timedelta(minutes=15))
    reopened.rehydrate(registry)

    live_registry = Registry(silence_after=timedelta(minutes=15))
    _apply_to_registry(live_registry, capture, at=datetime.fromtimestamp(1000.0, tz=UTC))

    trap = registry.trap(7)
    live_trap = live_registry.trap(7)
    assert trap.state is TrapState.ARMED  # state_after's own value, never a hardcoded FIRED
    assert live_trap.state is TrapState.ARMED  # CRITICAL A: the live path must read it too


def test_a_backlog_replayed_report_does_not_win_the_state_race_by_the_nodes_own_older_clock(
    tmp_path: Path,
) -> None:
    """WARNING A (verify-report-batch2a.md): the state race in
    `rehydrate()`'s `max(facts, key=lambda fact: fact.seq)` is decided by
    **store arrival**, never the node's own self-reported `node_epoch`
    (persisted, never read by the race). Pinned deliberately: `GET_BACKLOG`
    lets a node replay whatever it buffered while the link was down
    (`radio-gateway/spec.md`), so a stale report arriving late is
    designed-for, not exotic.

    Here the node raised a `FAULT` at its own clock-time 18:37:20 but could
    not send it until the link came back, by which point it had already
    sent a live `HEARTBEAT` (`armed`) at 18:45:20 -- eight minutes *later*
    by its own clock. The `FAULT` still arrives at the station last (`seq`
    2) once the backlog is pulled.

    **Correction (verify-report-batch2.md, WARNING 5): `received_epoch` is
    given a stepped-back value on purpose, not merely a later one.** The
    fixture used to give the FAULT a *later* `received_epoch` (2060.0) than
    the HEARTBEAT's (2000.0), which made `key=seq` and `key=at` agree on
    every ordering this fixture could produce -- the mutation this test
    exists to kill (`max(..., key=lambda fact: fact.seq)` -> `key=lambda
    fact: fact.at`) survived the whole 748-test suite despite this test's
    presence. `_ReportedFact.at` is `_epoch_to_datetime(row.received_epoch)`
    -- the station's own wall clock (`Clock.now()` is `system_now()`,
    `clock.py`), which is *not* guaranteed monotonic with `seq`: it is
    exactly the clock `clock_trust.py` exists because the station may not
    trust, pre-sync (S12), and chrony/timesyncd may step it *backward* once
    a real sync lands. The FAULT below arrives with a station clock reading
    that has since been stepped back below the HEARTBEAT's own -- `seq`
    (an autoincrement primary key, never revisited) still orders it last;
    `received_epoch` no longer agrees. Trusting `node_epoch` instead would
    mean trusting the *node's* clock, which the station has even less
    reason to -- the same "must not depend on the clock" reasoning
    `DEDUP_WINDOW`'s own comment already gives for count-bounded, not
    time-bounded, dedup. Switching to `node_epoch` is a real option but a
    bigger one (it would have to reckon with per-event `clock_trust`), not
    a one-line key swap -- so arrival-by-`seq` is the decision made and
    pinned here, not deferred silently, and it is now pinned against both
    alternatives this fixture can reach.
    """
    db_path = tmp_path / "station.db"
    store = Store(db_path)

    heartbeat = StationEvent(
        source="node",
        node_id=7,
        msg_type=payloads.HEARTBEAT,
        msg_id=1,
        node_epoch=1_000_000,  # the node's own clock: 18:45:20 in the report's own scenario
        tag=None,
        fields={"state": 1, "battery_mv": 3900, "targets_version": 2},  # 1 -> ARMED
        payload=b"",
    )
    store.append(heartbeat, received_epoch=2000.0, clock_trust="rtc")

    fault = StationEvent(
        source="node",
        node_id=7,
        msg_type=payloads.FAULT,
        msg_id=2,
        node_epoch=999_520,  # 8 minutes OLDER than the heartbeat above, by the node's own clock
        tag=None,
        fields={"code": 1, "detail": ""},
        payload=b"",
    )
    # Buffered on the node while the link was down, pulled later by
    # GET_BACKLOG -- arrives at the station last by seq (2, after the
    # heartbeat's 1), even though the node raised it first by its own
    # clock AND even though the station's own wall clock, stepped back by
    # chrony between the two appends, now reads it as *earlier* than the
    # heartbeat (1000.0 < 2000.0). key=seq and key=at now disagree.
    store.append(fault, received_epoch=1000.0, clock_trust="rtc")
    store.close()

    reopened = Store(db_path)
    registry = Registry(silence_after=timedelta(minutes=15))
    reopened.rehydrate(registry)

    trap = registry.trap(7)
    # Arrival (seq) wins: FAULT arrived last (seq 2), even though by the
    # node's own node_epoch it is the older report AND by the station's own
    # (stepped-back) received_epoch it now reads as the older one too.
    assert trap.state is TrapState.FAULT
    assert trap.last_seen == datetime.fromtimestamp(1000.0, tz=UTC)


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


def test_rehydrate_still_shows_the_state_of_an_already_acknowledged_capture(
    tmp_path: Path,
) -> None:
    """CRITICAL 1 (sdd-verify, field-station-p4 batch 2 — verify-report-2.md):
    this test previously asserted no trap existed at all for node 4, on
    the reasoning that an already-acknowledged capture leaves "nothing
    reported anything left to rehydrate". That reasoning is exactly the
    misreading verify-report-2.md's CRITICAL 1 names: *"S4's fact is the
    trap's most recent report — and a CAPTURED ... is a report, made by
    the node, about itself"* — and acknowledging one clears the capture
    *object* alone (`Registry.acknowledge()` only ever sets `trap.capture
    = None`), never the reported state. The live registry would show
    `state=fired` for this exact log whether or not the operator ever
    acknowledged it; rehydration must agree.
    """
    db_path = tmp_path / "station.db"
    store = Store(db_path)
    capture = StationEvent(
        source="node",
        node_id=4,
        msg_type=payloads.CAPTURED,
        msg_id=1,
        node_epoch=None,
        tag=TAG,
        fields={"state_after": 2},  # 2 -> FIRED
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

    trap = registry.trap(4)
    assert trap.state is TrapState.FIRED
    assert trap.capture is None  # acknowledged: the capture object itself does not resurface


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


def test_a_skipped_row_is_logged_at_error_with_the_node_id_and_a_traceback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """WARNING K(a) (verify-report-batch1-round2.md): `except Exception`
    was accepted as a broad catch specifically because every skip is
    logged with its node id and a traceback -- the *counted* half
    (`rehydration_errors`) is pinned five ways; the *logged* half was
    pinned by nobody, and mutation testing proved it: downgrading this log
    to `logger.debug("rehydrate: skipped a row")`, dropping both the node
    id and the traceback, left all 729 tests green.
    """
    db_path = tmp_path / "station.db"
    store = Store(db_path)
    store.append(
        StationEvent(
            source="node",
            node_id=7,
            msg_type=payloads.HEARTBEAT,
            msg_id=1,
            node_epoch=None,
            tag=None,
            fields={"state": 7, "battery_mv": 4000, "targets_version": 1},  # 7: no known TrapState
            payload=b"",
        ),
        received_epoch=1000.0,
        clock_trust="unset",
    )
    registry = Registry(silence_after=timedelta(minutes=15))

    with caplog.at_level(logging.DEBUG, logger="feeder_station.adapters.store"):
        store.rehydrate(registry)

    skip_records = [r for r in caplog.records if "skipping an unreadable row" in r.getMessage()]
    assert len(skip_records) == 1
    record = skip_records[0]
    assert record.levelno == logging.ERROR  # not downgraded to DEBUG
    assert "7" in record.getMessage()  # the node id -- not dropped
    assert record.exc_info is not None  # the traceback -- not dropped


def test_rehydration_errors_resets_each_call_not_accumulating(tmp_path: Path) -> None:
    """SUGGESTION E: three calls against one bad row must read 1, 1, 1,
    never 1, 2, 3.
    """
    db_path = tmp_path / "station.db"
    store = Store(db_path)
    store.append(
        StationEvent(
            source="node",
            node_id=7,
            msg_type=payloads.HEARTBEAT,
            msg_id=1,
            node_epoch=None,
            tag=None,
            fields={"state": 7, "battery_mv": 4000, "targets_version": 1},  # 7: no known TrapState
            payload=b"",
        ),
        received_epoch=1000.0,
        clock_trust="unset",
    )
    registry = Registry(silence_after=timedelta(minutes=15))

    store.rehydrate(registry)
    store.rehydrate(registry)
    store.rehydrate(registry)

    assert store.rehydration_errors == 1  # not 3 -- each call starts fresh


# --- WARNING C (verify-report-batch2a.md), WARNING J's own residual --------
# WARNING J closed "no readable state row at all" (`state` honestly `None`).
# It left open "the *newest* state-bearing row is unreadable, an older one
# is not" -- the older, readable report still wins the race and is shown as
# current, with nothing saying a fresher one existed and could not be read.


def test_a_skipped_newer_row_flags_the_older_survivor_as_possibly_stale(
    tmp_path: Path,
) -> None:
    """Two-row node: a readable HEARTBEAT (`ARMED`) first, then a FAULT
    whose `received_epoch` cannot be turned into a `datetime` (the same
    `OverflowError` CRITICAL A's own corruption technique produces) --
    the node's actual latest word, unreadable. The HEARTBEAT still wins
    the state race (WARNING J: a real, older report beats nothing at
    all) but the winner must now say it might not be current, not
    present `armed` as a plain fact the way `test_fault_uplink_updates_
    reported_state_to_fault` shows a *readable* FAULT would.
    """
    db_path = tmp_path / "station.db"
    store = Store(db_path)
    store.append(
        StationEvent(
            source="node",
            node_id=7,
            msg_type=payloads.HEARTBEAT,
            msg_id=1,
            node_epoch=None,
            tag=None,
            fields={"state": 1, "battery_mv": 4000, "targets_version": 1},  # 1 -> ARMED
            payload=b"",
        ),
        received_epoch=1000.0,
        clock_trust="unset",
    )
    store.append(
        StationEvent(
            source="node",
            node_id=7,
            msg_type=payloads.FAULT,
            msg_id=2,
            node_epoch=None,
            tag=None,
            fields={},
            payload=b"",
        ),
        received_epoch=1e20,  # unreadable: OverflowError inside _epoch_to_datetime
        clock_trust="unset",
    )
    store.close()

    reopened = Store(db_path)
    registry = Registry(silence_after=timedelta(minutes=15))
    reopened.rehydrate(registry)

    trap = registry.trap(7)
    # the older, readable report -- still the best fact there is
    assert trap.state is TrapState.ARMED
    assert trap.state_may_be_stale is True  # ... but a newer one existed and could not be read
    assert reopened.rehydration_errors == 1


def test_a_fresh_live_report_clears_the_possibly_stale_flag(tmp_path: Path) -> None:
    """The flag is a rehydration-time hedge, not a permanent mark: once the
    node reports again live, that report is by definition the newest thing
    the station has heard, so there is nothing left to be stale about.
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.restore_reported_state(7, TrapState.ARMED, at=datetime.now(UTC))
    registry.mark_state_possibly_stale(7)
    assert registry.trap(7).state_may_be_stale is True

    registry.heartbeat(7, state=TrapState.IDLE, at=datetime.now(UTC))

    assert registry.trap(7).state_may_be_stale is False


def test_trap_state_from_wire_maps_every_valid_byte_to_the_right_state() -> None:
    """WARNING B: the wire-order mapping, pinned end to end."""
    assert [trap_state_from_wire(v) for v in range(len(TrapState))] == [
        TrapState.IDLE,
        TrapState.ARMED,
        TrapState.FIRED,
        TrapState.FAULT,
    ]


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
