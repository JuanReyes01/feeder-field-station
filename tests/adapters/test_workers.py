"""``adapters/workers.py`` — the module that finally *starts* the four
drivers slices 5, 7, 8 and 9 left fully tested with nothing calling them
(this change's own tasks.md, slice 12: "Nothing in slices 1-11 ever
*starts* a thread").

``run_gateway_loop`` is exercised here as a plain function call, driven to
a stop by its own fake's side effect -- no real thread needed to prove the
wiring. ``run_scheduler_loop`` genuinely runs on a background thread,
because proving its 1-second tick is interruptible (design.md section 7,
the "Shutdown" row) needs a real ``threading.Event`` set from outside it.
``tests/test_app.py`` has the real end-to-end proof, through
``create_app``'s own lifespan, of all four drivers running together.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import groupby
from pathlib import Path

import pytest

from feeder_station.adapters.ingest import ingest_frame
from feeder_station.adapters.store import Store
from feeder_station.adapters.workers import (
    WorkerStatus,
    run_gateway_loop,
    run_scheduler_loop,
    run_supervised,
)
from feeder_station.config import StationConfig
from feeder_station.domain import payloads
from feeder_station.domain.clock_trust import ClockTrust
from feeder_station.domain.events import STATION_TIME_SYNC_WITHHELD
from feeder_station.domain.frames import FLAG_ACK_REQUIRED, Frame, decode, encode
from feeder_station.domain.traps import Registry, TrapState


class _FixedClock:
    def __init__(self, trust: ClockTrust = ClockTrust.NTP, at: datetime | None = None) -> None:
        self._trust = trust
        self._at = at or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._at

    def trust(self) -> ClockTrust:
        return self._trust


def _heartbeat_frame(node_id: int = 3, msg_id: int = 9, clock_source: int = 1) -> bytes:
    """``clock_source`` defaults to `1` (RTC/trusted) -- messages.md: "0
    unset, 1 RTC" -- payload[7], the byte the clock-trust bug fix
    (``adapters/workers.py``'s TIME_SYNC retry) reads back off the node's
    own latest stored `HEARTBEAT` row.
    """
    payload = bytes(
        [
            0x01,
            0xA8,
            0x0A,
            0x04,
            0x03,
            0x02,
            0x01,
            clock_source,
            0x09,
            0x00,
            0x05,
            0x00,
            0x08,
            0x07,
            0x06,
            0x05,
        ]
    )
    return encode(
        Frame(
            msg_type=payloads.HEARTBEAT,
            node_id=node_id,
            msg_id=msg_id,
            flags=FLAG_ACK_REQUIRED,
            payload=payload,
        )
    )


# --- run_gateway_loop: 12.0b's gateway half ---------------------------------


class _OneShotGateway:
    """Delivers one raw frame, then stops the loop itself on the next
    ``read_frames`` -- no real thread needed to drive this to an end.
    """

    def __init__(self, frame: bytes) -> None:
        self._frame = frame
        self._delivered = False
        self.write_calls: list[bytes] = []
        self.stop_event: threading.Event | None = None

    def ensure_connected(self, *, stop: Callable[[], bool]) -> bool:
        return True

    def read_frames(self, *, size: int = 4096) -> list[bytes]:
        if self._delivered:
            assert self.stop_event is not None
            self.stop_event.set()
            return []
        self._delivered = True
        return [self._frame]

    def write_frame(self, raw_frame: bytes) -> bool:
        self.write_calls.append(raw_frame)
        return True


def test_run_gateway_loop_ingests_a_frame_then_honours_stop(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    gateway = _OneShotGateway(_heartbeat_frame(node_id=3, msg_id=9))
    stop = threading.Event()
    gateway.stop_event = stop
    published: list[dict[str, object]] = []

    last_frame_arrival: dict[int, float] = {}
    run_gateway_loop(
        gateway,  # type: ignore[arg-type]
        store=store,
        registry=registry,
        registry_lock=threading.Lock(),
        publish=published.append,
        clock=_FixedClock(),
        stop=stop,
        last_frame_arrival=last_frame_arrival,
        record_payload_error=lambda: None,
    )

    assert stop.is_set()
    assert registry.trap(3).state is TrapState.ARMED  # decoded from the fixture payload
    assert published  # step 9 of the S9 ordering ran
    # CRITICAL 4 (sdd-verify): the gateway thread must record the arrival
    # itself -- this is the fact `run_scheduler_loop`'s own `responded`
    # check now compares against, replacing the unsound 15-minute window.
    assert 3 in last_frame_arrival
    # step 6/7: the frame asked for an ACK (FLAG_ACK_REQUIRED) -- one must
    # have gone back out over the same gateway.
    assert any(decode(raw).msg_type == payloads.ACK for raw in gateway.write_calls)


class _CrashingGateway:
    def ensure_connected(self, *, stop: Callable[[], bool]) -> bool:
        raise RuntimeError("boom -- simulated dead radio")

    def read_frames(self, *, size: int = 4096) -> list[bytes]:  # pragma: no cover - unreachable
        return []

    def write_frame(self, raw_frame: bytes) -> bool:  # pragma: no cover - unreachable
        return True


# --- run_supervised: 12.0c's health-visibility half -------------------------


def test_run_supervised_records_an_escaping_exception_instead_of_raising() -> None:
    """The whole point of 12.0c: a dead worker must be observable, not just
    silently gone. This is the function `/health` reads through
    ``WorkerStatus``.
    """
    status = WorkerStatus()

    def _body() -> None:
        raise RuntimeError("boom -- simulated dead radio")

    run_supervised(status, _body)  # must not raise

    assert status.last_error is not None
    assert "boom" in status.last_error


def test_run_supervised_leaves_last_error_none_on_a_clean_stop() -> None:
    status = WorkerStatus()
    run_supervised(status, lambda: None)
    assert status.last_error is None


def test_a_crashing_gateway_loop_is_caught_by_run_supervised(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    status = WorkerStatus()

    run_supervised(
        status,
        lambda: run_gateway_loop(
            _CrashingGateway(),  # type: ignore[arg-type]
            store=store,
            registry=registry,
            registry_lock=threading.Lock(),
            publish=lambda event: None,
            clock=_FixedClock(),
            stop=threading.Event(),
            last_frame_arrival={},
            record_payload_error=lambda: None,
        ),
    )

    assert status.last_error is not None
    assert "boom" in status.last_error


# --- run_scheduler_loop: 12.0b's scheduler half, real thread ---------------


class _SpyGateway:
    """`last_frame_arrival`/`responsive_node_ids` (CRITICAL 5, batch 4) are
    optional: when set, a responsive node's poll marks its own "arrival,"
    simulating the reply `run_gateway_loop` would record, single-threaded.
    """

    def __init__(
        self,
        last_frame_arrival: dict[int, float] | None = None,
        responsive_node_ids: set[int] | None = None,
    ) -> None:
        self.written: list[bytes] = []
        self._arrival = last_frame_arrival
        self._responsive_node_ids = responsive_node_ids or set()

    def ensure_connected(self, *, stop: Callable[[], bool]) -> bool:
        return True

    def read_frames(self, *, size: int = 4096) -> list[bytes]:  # pragma: no cover - unused here
        return []

    def write_frame(self, raw_frame: bytes) -> bool:
        self.written.append(raw_frame)
        if self._arrival is not None:
            frame = decode(raw_frame)
            is_backlog = frame.msg_type == payloads.GET_BACKLOG
            if is_backlog and frame.node_id in self._responsive_node_ids:
                self._arrival[frame.node_id] = time.time()
        return True


def test_run_scheduler_loop_polls_backlog_and_resends_a_due_command(tmp_path: Path) -> None:
    """The scheduler half of 12.0a's own scenario: a due DISARM must be
    resent, and the known roster must be polled for backlog -- both of
    slice 7's and slice 8's own orphaned drivers, run from one thread.
    """
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.command_disarm(7, at=datetime(2026, 1, 1, tzinfo=UTC))
    gateway = _SpyGateway()
    config = StationConfig.load(argv=[], env={})
    stop = threading.Event()

    thread = threading.Thread(
        target=run_scheduler_loop,
        kwargs={
            "store": store,
            "gateway": gateway,
            "registry": registry,
            "registry_lock": threading.Lock(),
            "clock": _FixedClock(trust=ClockTrust.NTP),
            "config": config,
            "stop": stop,
            "tick_s": 0.01,
        },
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 2.0
        msg_types: set[int] = set()
        while time.monotonic() < deadline:
            msg_types = {decode(raw).msg_type for raw in gateway.written}
            if payloads.GET_BACKLOG in msg_types and payloads.DISARM in msg_types:
                break
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert payloads.GET_BACKLOG in msg_types
    assert payloads.DISARM in msg_types


def test_run_scheduler_loop_stops_promptly_even_mid_tick(tmp_path: Path) -> None:
    """design.md section 7's "Shutdown" row: a clean `SIGTERM` path is what
    makes `Restart=always` a recovery mechanism, not a corruption
    generator. A long `tick_s` must not block shutdown responsiveness --
    `threading.Event.wait` is what makes that true.
    """
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    gateway = _SpyGateway()
    config = StationConfig.load(argv=[], env={})
    stop = threading.Event()

    thread = threading.Thread(
        target=run_scheduler_loop,
        kwargs={
            "store": store,
            "gateway": gateway,
            "registry": registry,
            "registry_lock": threading.Lock(),
            "clock": _FixedClock(),
            "config": config,
            "stop": stop,
            "tick_s": 30.0,  # would hang for 30s if `stop` were not honoured promptly
        },
        daemon=True,
    )
    thread.start()
    time.sleep(0.05)
    started_stop = time.monotonic()
    stop.set()
    thread.join(timeout=2.0)
    elapsed = time.monotonic() - started_stop

    assert not thread.is_alive()
    assert elapsed < 1.0


# --- CRITICAL 3 (sdd-verify, field-station-p4): TIME_SYNC resumes ----------


class _FlippingClock:
    """A `Clock` double whose trust starts `UNSET` and can be flipped to
    `NTP` on demand -- the exact transition
    `clock-trust/spec.md`'s "TIME_SYNC resumes once trusted" scenario
    names, driven through the real `run_scheduler_loop`, not through a
    single `attempt_time_sync` call in isolation.
    """

    def __init__(self, at: datetime) -> None:
        self._at = at
        self._trust = ClockTrust.UNSET

    def now(self) -> datetime:
        return self._at

    def trust(self) -> ClockTrust:
        return self._trust

    def flip_to_ntp(self) -> None:
        self._trust = ClockTrust.NTP


def test_time_sync_resumes_once_the_scheduler_loop_sees_a_trusted_clock(
    tmp_path: Path,
) -> None:
    """CRITICAL 3: `run_scheduler_loop` discarded `attempt_time_sync`'s
    return value and marked a node synced even when the sync was withheld
    -- boot with an unset clock (S12's own scenario: no internet yet), and
    no node was ever offered `TIME_SYNC` again once NTP came up, however
    long the station ran. The bug lived entirely in this loop, not in
    `attempt_time_sync` itself (already correctly returning `bool` --
    see `tests/adapters/test_clock.py`), which is why this drives the real
    loop on a real thread rather than calling that function directly.
    """
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(node_id=3, state=TrapState.IDLE, at=datetime(2026, 1, 1, tzinfo=UTC))
    gateway = _SpyGateway()
    clock = _FlippingClock(datetime(2026, 1, 1, tzinfo=UTC))
    config = StationConfig.load(argv=[], env={})
    stop = threading.Event()

    thread = threading.Thread(
        target=run_scheduler_loop,
        kwargs={
            "store": store,
            "gateway": gateway,
            "registry": registry,
            "registry_lock": threading.Lock(),
            "clock": clock,
            "config": config,
            "stop": stop,
            "tick_s": 0.01,
        },
        daemon=True,
    )
    thread.start()
    try:
        # While unset: withheld, recorded, never a frame.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and store.latest(3, STATION_TIME_SYNC_WITHHELD) is None:
            time.sleep(0.01)
        assert store.latest(3, STATION_TIME_SYNC_WITHHELD) is not None
        assert not any(decode(raw).msg_type == payloads.TIME_SYNC for raw in gateway.written)

        clock.flip_to_ntp()

        # Once trusted: the very next tick must offer TIME_SYNC again.
        deadline = time.monotonic() + 1.0
        resumed = False
        while time.monotonic() < deadline:
            if any(decode(raw).msg_type == payloads.TIME_SYNC for raw in gateway.written):
                resumed = True
                break
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert resumed, "TIME_SYNC never resumed after the clock became trusted"


# --- The clock-trust bug fix: TIME_SYNC retry driven by the node's own -----
# --- reported clock, never by a station-side "we sent one once" flag ------


def _seed_heartbeat(
    store: Store, registry: Registry, *, node_id: int, clock_source: int, msg_id: int = 1
) -> None:
    """Ingests one real `HEARTBEAT` frame so `store.latest(node_id,
    payloads.HEARTBEAT)` has a row to read back -- the clock-trust bug fix
    (`adapters/workers.py`'s TIME_SYNC retry) drives its retry off exactly
    that row, never off `Registry`/`Trap` (see that function's own
    docstring for why), so seeding through the real ingest path is what
    actually proves the fix reads what a real `HEARTBEAT` writes, not a
    hand-built double of it.
    """
    ingest_frame(
        _heartbeat_frame(node_id=node_id, msg_id=msg_id, clock_source=clock_source),
        store=store,
        ack_port=_SpyGateway(),
        registry=registry,
        publish=lambda _event: None,
        received_epoch=datetime(2026, 1, 1, tzinfo=UTC).timestamp(),
        clock_trust=ClockTrust.NTP.value,
    )


def test_time_sync_keeps_retrying_a_node_whose_own_clock_stays_unset(tmp_path: Path) -> None:
    """The bug this fix answers (docs/pending.md): a *sent* TIME_SYNC used
    to mark a node synced forever, whether or not the node ever applied
    it -- a bench day with the station restarted about five times, the
    node rebooting eighteen times and the gateway going deaf at least once
    left `clock_epoch: 0`/`clock_source: 0` (unset) across 2,685
    heartbeats, having each been offered TIME_SYNC exactly once. This is
    the regression proof: a node whose own last HEARTBEAT still reports
    `clock_source: 0` must be attempted again on the next cadence, not
    once ever.
    """
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    _seed_heartbeat(store, registry, node_id=3, clock_source=0)
    gateway = _SpyGateway()
    config = StationConfig(
        heartbeat_interval=timedelta(seconds=0.05),  # fast cadence for a short-running test
        channel_budget_frames_per_s=1000.0,
    )
    stop = threading.Event()

    thread = threading.Thread(
        target=run_scheduler_loop,
        kwargs={
            "store": store,
            "gateway": gateway,
            "registry": registry,
            "registry_lock": threading.Lock(),
            "clock": _FixedClock(trust=ClockTrust.NTP),
            "config": config,
            "stop": stop,
            "tick_s": 0.01,
        },
        daemon=True,
    )
    thread.start()
    time.sleep(0.3)  # several multiples of the 0.05 s cadence
    stop.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    time_sync_count = sum(1 for raw in gateway.written if decode(raw).msg_type == payloads.TIME_SYNC)
    assert time_sync_count >= 3, (
        f"only {time_sync_count} TIME_SYNC attempt(s) in 0.3s at a 0.05s cadence -- a node "
        "whose own clock stays unset must keep being retried, not offered once and forgotten"
    )


def test_time_sync_stops_once_the_node_itself_reports_a_trusted_clock(tmp_path: Path) -> None:
    """The other half of the fix: a node whose own last HEARTBEAT already
    reports `clock_source: 1` (RTC) needs no TIME_SYNC at all -- this is
    the fact the retry is driven off now, never a station-side flag that
    merely records a frame having left the port.
    """
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    _seed_heartbeat(store, registry, node_id=3, clock_source=1)
    gateway = _SpyGateway()
    config = StationConfig(
        heartbeat_interval=timedelta(seconds=0.02),
        channel_budget_frames_per_s=1000.0,
    )
    stop = threading.Event()

    thread = threading.Thread(
        target=run_scheduler_loop,
        kwargs={
            "store": store,
            "gateway": gateway,
            "registry": registry,
            "registry_lock": threading.Lock(),
            "clock": _FixedClock(trust=ClockTrust.NTP),
            "config": config,
            "stop": stop,
            "tick_s": 0.01,
        },
        daemon=True,
    )
    thread.start()
    time.sleep(0.3)
    stop.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert not any(decode(raw).msg_type == payloads.TIME_SYNC for raw in gateway.written), (
        "a node already reporting a trusted clock of its own must not be offered TIME_SYNC at all"
    )


# --- CRITICAL 4 (sdd-verify, field-station-p4): the rotation is paced ------


class _RealClock:
    """A `Clock` double that reads the real wall clock -- CRITICAL 4's own
    fix is paced by real elapsed time (`config.heartbeat_interval`), so
    proving the pacing holds needs a clock that actually advances, unlike
    every other fake in this file.
    """

    def __init__(self, trust: ClockTrust = ClockTrust.NTP) -> None:
        self._trust = trust

    def now(self) -> datetime:
        return datetime.now(UTC)

    def trust(self) -> ClockTrust:
        return self._trust


def test_backlog_rotation_persists_across_ticks_and_is_paced(tmp_path: Path) -> None:
    """CRITICAL 4: before this fix, `run_scheduler_loop` rebuilt
    `backlog.start()` fresh every 1 s tick and swept the *whole* roster
    (the now-deleted `run_rotation_round`) inside it -- measured 13.3-40
    GET_BACKLOG frames/s at ten known traps (verify-report.md). This
    drives the real loop, on a real thread, against ten traps that never
    respond -- the worst case the report measured -- and proves the real,
    paced rate instead: one poll at most per `config.heartbeat_interval`,
    never a per-tick sweep.

    **CRITICAL 5 (sdd-verify field-station-p4, batch 4): "persists... and
    is paced" promises more than this checks** -- it counts frames, never
    poll order. Kept as-is, a coarse rate regression guard; order is
    proved by the sibling test immediately below instead.
    """
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    for node_id in range(10):
        registry.heartbeat(node_id, TrapState.IDLE, at=datetime(2026, 1, 1, tzinfo=UTC))
    gateway = _SpyGateway()
    poll_interval_s = 0.1
    config = StationConfig(
        heartbeat_interval=timedelta(seconds=poll_interval_s),
        # CRITICAL 3 (sdd-verify field-station-p4): this test isolates
        # GET_BACKLOG's own per-node cadence (CRITICAL 4's concern); the
        # new station-wide budget is a second, independent throttle this
        # test is not about (see the new GET_BACKLOG-alone and TIME_SYNC-
        # alone budget tests below for that), and with ten trusted-clock
        # nodes all wanting TIME_SYNC on tick 1, the default budget would
        # starve GET_BACKLOG entirely for tens of seconds.
        channel_budget_frames_per_s=1000.0,
    )
    stop = threading.Event()

    thread = threading.Thread(
        target=run_scheduler_loop,
        kwargs={
            "store": store,
            "gateway": gateway,
            "registry": registry,
            "registry_lock": threading.Lock(),
            "clock": _RealClock(),
            "config": config,
            "stop": stop,
            "tick_s": 0.01,
        },
        daemon=True,
    )
    started = time.monotonic()
    thread.start()
    time.sleep(1.0)
    stop.set()
    thread.join(timeout=2.0)
    elapsed = time.monotonic() - started

    get_backlog_count = sum(
        1 for raw in gateway.written if decode(raw).msg_type == payloads.GET_BACKLOG
    )
    frames_per_second = get_backlog_count / elapsed

    # Before this fix: 30-40 GET_BACKLOG/s at ten known, unresponsive traps
    # (verify-report.md, CRITICAL 4, measured on this same machine). Paced
    # at one poll per `poll_interval_s`, the rate must land near
    # `1 / poll_interval_s` (~10/s here) -- nowhere close to a full-roster
    # sweep repeated every tick.
    assert get_backlog_count >= 3, (
        f"pacing produced only {get_backlog_count} polls in {elapsed:.2f}s"
    )
    assert frames_per_second < 15, f"got {frames_per_second:.1f} GET_BACKLOG frames/s"


def test_backlog_rotation_skips_a_silent_node_within_its_retry_budget_and_continues(
    tmp_path: Path,
) -> None:
    """CRITICAL 5 (sdd-verify field-station-p4, batch 4): moves the deleted
    `run_rotation_round`'s poll-order coverage onto its production
    replacement, `run_scheduler_loop`, the sole caller of `poll_node`. Ten
    known traps, node 5 never responding; the other nine have their reply
    simulated via `_SpyGateway`'s arrival hook. `retry_budget=3`
    (`config.backlog_retry_budget`'s default).

    Empirically confirmed (not assumed from the deleted primitive's own
    example, which does not reproduce here): every node's *first-ever*
    poll registers as unheard regardless of responsiveness -- no
    `last_polled_at` baseline exists yet -- so lap 1 is skipped, and this
    asserts laps 2-3, where the real distinction holds: node 5 retried
    exactly `retry_budget` times, every other node exactly once, repeating
    lap after lap -- "one silent node does not block the rotation,"
    radio-gateway spec.md.
    """
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    node_ids = tuple(range(1, 11))
    silent_node = 5
    for node_id in node_ids:
        registry.heartbeat(node_id, TrapState.IDLE, at=datetime(2026, 1, 1, tzinfo=UTC))

    last_frame_arrival: dict[int, float] = {}
    responsive_node_ids = set(node_ids) - {silent_node}
    gateway = _SpyGateway(last_frame_arrival, responsive_node_ids)
    config = StationConfig(
        heartbeat_interval=timedelta(seconds=0.03),
        channel_budget_frames_per_s=1000.0,
    )
    stop = threading.Event()

    thread = threading.Thread(
        target=run_scheduler_loop,
        kwargs={
            "store": store,
            "gateway": gateway,
            "registry": registry,
            "registry_lock": threading.Lock(),
            "clock": _RealClock(trust=ClockTrust.UNSET),
            "config": config,
            "stop": stop,
            "tick_s": 0.005,
            "last_frame_arrival": last_frame_arrival,
        },
        daemon=True,
    )
    thread.start()
    try:
        deadline = time.monotonic() + 5.0
        # 10 runs for the transient first lap + 20 for two full steady
        # laps, plus one more run starting so the 30th is known to have
        # ended (its own successor run has begun).
        while time.monotonic() < deadline:
            order = [decode(raw).node_id for raw in gateway.written]
            if sum(1 for _ in groupby(order)) >= 31:
                break
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    order = [decode(raw).node_id for raw in gateway.written]
    observed_runs = [(node_id, len(list(g))) for node_id, g in groupby(order)]

    assert len(observed_runs) >= 31, (
        f"only {len(observed_runs)} poll-runs observed; not enough to reach steady state"
    )
    first_lap_ids = [node_id for node_id, _ in observed_runs[:10]]
    assert first_lap_ids == list(node_ids), (
        "the first lap must still visit every node once, in round-robin order, "
        "even though every node's retry count during it is a first-contact artifact"
    )

    steady_lap = [(node_id, 3 if node_id == silent_node else 1) for node_id in node_ids]
    assert observed_runs[10:30] == steady_lap * 2, (
        "laps 2 and 3 must each retry only the silent node, exactly retry_budget times, "
        "visit every other node exactly once, and repeat identically -- proving it "
        "neither blocks the rotation nor is permanently dropped from it"
    )


# --- CRITICAL 3 (sdd-verify field-station-p4, batch 3a): the shared budget
# --- gates GET_BACKLOG and TIME_SYNC too, not just command resends --------


def test_get_backlog_alone_holds_the_channel_budget_measured_for_real(tmp_path: Path) -> None:
    """3.6 (sdd-verify field-station-p4, batch 3a) -- **rewritten, not
    deleted**. Before: `test_default_backlog_pacing_holds_the_stations_
    own_airtime_budget` computed `1/300 * 0.37 < 0.01` from two module
    constants -- CRITICAL 3's own report: "it measures nothing... is
    arithmetic asserting arithmetic." This keeps that test's original,
    honestly-scoped subject -- GET_BACKLOG's *own* contribution alone,
    matching its old docstring -- but now drives the real
    `run_scheduler_loop` on a real thread and counts real frames off the
    wire; `test_station_total_frame_rate_holds_the_channel_budget_off_
    the_real_wire` (tests/test_cli.py) owns the *total* across all three
    transmitters, which nothing before this batch measured at all.

    Ten idle traps (no pending command, clock left `UNSET` so TIME_SYNC
    never actually transmits) isolates GET_BACKLOG as the only category
    competing for the shared budget -- and `heartbeat_interval` is set
    fast enough that, without the shared budget gating `poll_node`, this
    would poll far above the configured rate (as
    `test_backlog_rotation_persists_across_ticks_and_is_paced` above
    already proves for the per-node cadence alone, with the budget
    deliberately loosened there). Here the budget itself is the binding
    constraint being measured.
    """
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    for node_id in range(10):
        registry.heartbeat(node_id, TrapState.IDLE, at=datetime(2026, 1, 1, tzinfo=UTC))
    gateway = _SpyGateway()
    channel_budget_frames_per_s = 2.0
    config = StationConfig(
        heartbeat_interval=timedelta(seconds=0.02),  # would poll far faster than the budget
        channel_budget_frames_per_s=channel_budget_frames_per_s,
    )
    stop = threading.Event()

    thread = threading.Thread(
        target=run_scheduler_loop,
        kwargs={
            "store": store,
            "gateway": gateway,
            "registry": registry,
            "registry_lock": threading.Lock(),
            "clock": _RealClock(trust=ClockTrust.UNSET),
            "config": config,
            "stop": stop,
            "tick_s": 0.005,
        },
        daemon=True,
    )
    started = time.monotonic()
    thread.start()
    time.sleep(2.0)
    stop.set()
    thread.join(timeout=2.0)
    elapsed = time.monotonic() - started

    assert not thread.is_alive()
    written_types = {decode(raw).msg_type for raw in gateway.written}
    assert written_types == {payloads.GET_BACKLOG}  # the only category competing here
    frames_per_second = len(gateway.written) / elapsed

    # A generous margin over the *configured* rate (this test accelerates
    # it for speed -- messages.md's real ~0.027/s is proven separately,
    # off the real default config, in tests/test_cli.py): the bucket
    # starts with `pacing.DEFAULT_CAPACITY` free tokens, so a short
    # window's average sits above the asymptotic rate -- the point here is
    # "bounded by the budget", not "already converged to it".
    assert frames_per_second < channel_budget_frames_per_s * 3, (
        f"got {frames_per_second:.2f} GET_BACKLOG frames/s, budget is "
        f"{channel_budget_frames_per_s}/s"
    )


def test_time_sync_alone_is_capped_by_the_shared_budget(tmp_path: Path) -> None:
    """CRITICAL 3: ten nodes all newly eligible for TIME_SYNC on the very
    first tick (a trusted clock, nothing synced yet) used to attempt every
    one of them in that single tick -- unbounded by anything but the
    roster size. This isolates TIME_SYNC as the only category competing
    for the shared budget (no pending commands, GET_BACKLOG paced far
    slower than the budget by a long `heartbeat_interval`) and proves the
    same shared bucket caps it too, not just GET_BACKLOG and command
    resends.
    """
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    for node_id in range(10):
        registry.heartbeat(node_id, TrapState.IDLE, at=datetime(2026, 1, 1, tzinfo=UTC))
    gateway = _SpyGateway()
    config = StationConfig(
        heartbeat_interval=timedelta(minutes=5),  # GET_BACKLOG must not compete here
        channel_budget_frames_per_s=2.0,
    )
    stop = threading.Event()

    thread = threading.Thread(
        target=run_scheduler_loop,
        kwargs={
            "store": store,
            "gateway": gateway,
            "registry": registry,
            "registry_lock": threading.Lock(),
            "clock": _RealClock(trust=ClockTrust.NTP),
            "config": config,
            "stop": stop,
            "tick_s": 0.005,
        },
        daemon=True,
    )
    thread.start()
    time.sleep(0.05)  # one tick's worth of real time -- well under a refill period
    stop.set()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
    time_sync_count = sum(
        1 for raw in gateway.written if decode(raw).msg_type == payloads.TIME_SYNC
    )
    # `pacing.DEFAULT_CAPACITY` tokens start the bucket full -- at most that
    # many TIME_SYNC frames can go out before the very first refill, however
    # many of the ten nodes are simultaneously eligible.
    assert time_sync_count <= 3, f"{time_sync_count} of 10 nodes synced in one tick"
    assert 0 < time_sync_count, "the budget must not refuse every attempt either"


# --- CRITICAL 4 (sdd-verify field-station-p4, batch 3b): the withheld
# --- TIME_SYNC path's own write cadence -------------------------------------


@pytest.mark.parametrize(
    ("node_count", "window_s"),
    [(3, 0.4), (3, 0.8), (10, 0.4), (10, 0.8)],
)
def test_withheld_time_sync_rows_are_bounded_by_the_heartbeat_cadence_not_the_tick_rate(
    tmp_path: Path, node_count: int, window_s: float
) -> None:
    """CRITICAL 4: before this fix, every unsynced node was re-attempted on
    *every* tick with an unset clock, and `attempt_time_sync` unconditionally
    writes a `STATION_TIME_SYNC_WITHHELD` row before returning `False`
    (clock-trust/spec.md's own "MUST log each withheld send" -- the log
    itself is correct; the defect is that the scheduler made every node due
    once per tick, not once per send). Measured at production settings: 10
    traps, a 1 s tick, unset clock -> 10.0 fsync'd rows/s, ~189 MB/day
    (verify-report-2.md).

    This is property-shaped, not "at least one row exists" --
    `test_time_sync_resumes_once_the_scheduler_loop_sees_a_trusted_clock`
    above already covers that, correctly, and is untouched by this task. The
    property here is a *rate* bound that must hold regardless of roster size
    (parametrized 3 and 10 nodes) or how long the loop has been running: a
    `tick_s` far shorter than `window_s` means each window covers many tens
    of ticks, so a fix that only capped a *count* per run (rather than a
    genuine per-node cadence) would still show a rate that rises with a
    longer window at the same roster size -- exactly what the two `window_s`
    values per `node_count` are here to catch.
    """
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    for node_id in range(node_count):
        registry.heartbeat(node_id, TrapState.IDLE, at=datetime(2026, 1, 1, tzinfo=UTC))
    gateway = _SpyGateway()
    heartbeat_interval_s = 0.15
    config = StationConfig(
        heartbeat_interval=timedelta(seconds=heartbeat_interval_s),
        # Isolates the withheld-log cadence itself, not the shared airtime
        # budget (CRITICAL 3, already proven separately) -- a withheld
        # attempt spends no token either way (see workers.py's own
        # `would_send` gate), so this value is irrelevant to what this test
        # measures.
        channel_budget_frames_per_s=1000.0,
    )
    stop = threading.Event()

    thread = threading.Thread(
        target=run_scheduler_loop,
        kwargs={
            "store": store,
            "gateway": gateway,
            "registry": registry,
            "registry_lock": threading.Lock(),
            "clock": _RealClock(trust=ClockTrust.UNSET),
            "config": config,
            "stop": stop,
            "tick_s": 0.01,  # far shorter than heartbeat_interval_s: many ticks per window
        },
        daemon=True,
    )
    started = time.monotonic()
    thread.start()
    time.sleep(window_s)
    stop.set()
    thread.join(timeout=2.0)
    elapsed = time.monotonic() - started

    assert not thread.is_alive()
    withheld_count = store._conn.execute(
        "SELECT COUNT(*) FROM event WHERE msg_type = ?", (STATION_TIME_SYNC_WITHHELD,)
    ).fetchone()[0]
    rows_per_second = withheld_count / elapsed

    # Attempting each unsynced node at most once per `heartbeat_interval_s`
    # bounds the rate to roughly `node_count / heartbeat_interval_s`,
    # independent of `tick_s` -- a 2x safety margin absorbs the one extra
    # immediate attempt each node gets at boot. The old, unbounded behaviour
    # attempted every node on every 0.01s tick (`node_count / 0.01` --
    # 200-1000/s here), an order of magnitude past this bound either way.
    max_rows_per_second = 2.0 * (node_count / heartbeat_interval_s)
    assert rows_per_second <= max_rows_per_second, (
        f"{withheld_count} withheld rows in {elapsed:.2f}s = {rows_per_second:.1f} rows/s, "
        f"expected at most {max_rows_per_second:.1f} rows/s for {node_count} nodes"
    )


# --- Scheduler-throttle bug (bench repro, docs/pending.md): a large ---------
# --- `channel_budget_frames_per_s` must not let the loop monopolise --------
# --- `registry_lock` ---------------------------------------------------


class _BlockingWriteGateway:
    """A `write_frame` that blocks until the test releases it -- simulating
    a real serial write stalled on a full kernel TX buffer with nothing
    draining it (pyserial's own `write_timeout=None` default,
    `adapters/serial_gateway.py::_default_open_port`). This is the bench's
    own mechanism, not a stand-in for it: a real write can genuinely block
    forever, and the only question this test asks is whether
    `registry_lock` is held while that happens.
    """

    def __init__(self) -> None:
        self.write_started = threading.Event()
        self.release_write = threading.Event()
        self.written: list[bytes] = []

    def ensure_connected(self, *, stop: Callable[[], bool]) -> bool:
        return True

    def read_frames(self, *, size: int = 4096) -> list[bytes]:  # pragma: no cover - unused here
        return []

    def write_frame(self, raw_frame: bytes) -> bool:
        self.write_started.set()
        self.release_write.wait(timeout=5.0)
        self.written.append(raw_frame)
        return True


def test_a_blocked_time_sync_send_does_not_monopolise_the_registry_lock(
    tmp_path: Path,
) -> None:
    """docs/pending.md's own bench repro: `--channel-budget-frames-per-s 20`
    hung the real station -- one `/health` succeeded, then nothing responded
    and no event was ingested again, while the default budget (~0.027/s)
    stayed healthy indefinitely. Consequence named in that same report:
    "TIME_SYNC cannot reach the node." The reported mechanism: the token
    bucket is mutated under `registry_lock`, and with tokens always
    available "the scheduler loop stops waiting and spins holding that
    lock, starving the HTTP handlers and the ingest path" -- the budget
    silently doubling as the loop's own throttle, which it must never be
    (see `run_scheduler_loop`'s own docstring for the full mechanism: a
    real write has no timeout and can block forever once a high budget
    keeps every tick's burst full).

    A trusted clock and no pending command isolates TIME_SYNC as the one
    category that ever calls `gateway.write_frame` here: `_ROTATIONS[0]`
    (`rotation_index` starts at `0`) tries "commands" first (a no-op, no
    trap has a pending command) then "time_sync" -- before "poll" ever gets
    a turn this tick. `test_a_blocked_poll_send_does_not_monopolise_the_
    registry_lock` below isolates "poll" the same way, with the clock
    `UNSET` instead so TIME_SYNC never reaches the gateway at all.

    Proved honestly, not by a stopwatch: `_BlockingWriteGateway.write_frame`
    blocks until released, and the real property under test -- "the loop
    yields, and does not hold `registry_lock` while a send is in flight" --
    is asserted by a *second* thread acquiring the very same lock, with a
    bounded timeout, *while the fake write is still deliberately blocked*.
    A large budget (20/s, the bench's own reported value) guarantees a send
    is attempted on the very first tick rather than left to chance.
    """
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(1, TrapState.IDLE, at=datetime(2026, 1, 1, tzinfo=UTC))
    gateway = _BlockingWriteGateway()
    registry_lock = threading.Lock()
    config = StationConfig(
        heartbeat_interval=timedelta(seconds=0.01),
        channel_budget_frames_per_s=20.0,
    )
    stop = threading.Event()

    thread = threading.Thread(
        target=run_scheduler_loop,
        kwargs={
            "store": store,
            "gateway": gateway,
            "registry": registry,
            "registry_lock": registry_lock,
            "clock": _FixedClock(trust=ClockTrust.NTP),
            "config": config,
            "stop": stop,
            "tick_s": 0.01,
        },
        daemon=True,
    )
    thread.start()
    acquired = False
    try:
        assert gateway.write_started.wait(timeout=2.0), (
            "the loop never attempted a send -- this test proves nothing "
            "about the lock without one in flight"
        )

        # The fake write is still blocked here, deliberately. A bounded
        # timeout, not zero: this is a real inter-thread race between this
        # acquire and the scheduler thread's own release-and-reacquire
        # around other, unrelated work, not a claim that the lock is *never*
        # briefly held anywhere in the same tick.
        acquired = registry_lock.acquire(timeout=1.0)
        if acquired:
            registry_lock.release()
    finally:
        gateway.release_write.set()
        stop.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert acquired, (
        "registry_lock was still held while a TIME_SYNC send was blocked in "
        "gateway.write_frame() -- the channel budget is deciding how often "
        "the loop runs, not just what it may send"
    )
    assert decode(gateway.written[0]).msg_type == payloads.TIME_SYNC


def test_a_blocked_poll_send_does_not_monopolise_the_registry_lock(
    tmp_path: Path,
) -> None:
    """The sibling of the TIME_SYNC test above, isolating "poll"
    (`GET_BACKLOG`) as the one category that reaches `gateway.write_frame`:
    the clock is left `UNSET`, so TIME_SYNC only ever takes its withheld
    path (a `store` write, never a `gateway` one -- see `attempt_time_sync`),
    and no trap has a pending command, so "commands" stays a no-op. Whatever
    the rotation order, "poll" is the only category left that can ever
    block this loop's own gateway.
    """
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(1, TrapState.IDLE, at=datetime(2026, 1, 1, tzinfo=UTC))
    gateway = _BlockingWriteGateway()
    registry_lock = threading.Lock()
    config = StationConfig(
        heartbeat_interval=timedelta(seconds=0.01),
        channel_budget_frames_per_s=20.0,
    )
    stop = threading.Event()

    thread = threading.Thread(
        target=run_scheduler_loop,
        kwargs={
            "store": store,
            "gateway": gateway,
            "registry": registry,
            "registry_lock": registry_lock,
            "clock": _FixedClock(trust=ClockTrust.UNSET),
            "config": config,
            "stop": stop,
            "tick_s": 0.01,
        },
        daemon=True,
    )
    thread.start()
    acquired = False
    try:
        assert gateway.write_started.wait(timeout=2.0), (
            "the loop never attempted a send -- this test proves nothing "
            "about the lock without one in flight"
        )

        acquired = registry_lock.acquire(timeout=1.0)
        if acquired:
            registry_lock.release()
    finally:
        gateway.release_write.set()
        stop.set()
        thread.join(timeout=2.0)

    assert not thread.is_alive()
    assert acquired, (
        "registry_lock was still held while a GET_BACKLOG send was blocked "
        "in gateway.write_frame() -- the channel budget is deciding how "
        "often the loop runs, not just what it may send"
    )
    assert decode(gateway.written[0]).msg_type == payloads.GET_BACKLOG
