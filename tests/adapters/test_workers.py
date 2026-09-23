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
from pathlib import Path

from feeder_station.adapters.store import Store
from feeder_station.adapters.workers import (
    WorkerStatus,
    run_gateway_loop,
    run_scheduler_loop,
    run_supervised,
)
from feeder_station.config import DEFAULT_HEARTBEAT_INTERVAL, StationConfig
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


def _heartbeat_frame(node_id: int = 3, msg_id: int = 9) -> bytes:
    payload = bytes(
        [
            0x01,
            0xA8,
            0x0A,
            0x04,
            0x03,
            0x02,
            0x01,
            0x01,
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
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def ensure_connected(self, *, stop: Callable[[], bool]) -> bool:
        return True

    def read_frames(self, *, size: int = 4096) -> list[bytes]:  # pragma: no cover - unused here
        return []

    def write_frame(self, raw_frame: bytes) -> bool:
        self.written.append(raw_frame)
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
    (`run_rotation_round`) inside it -- measured 13.3-40 GET_BACKLOG
    frames/s at ten known traps (verify-report.md). This drives the real
    loop, on a real thread, against ten traps that never respond -- the
    worst case the report measured -- and proves the real, paced rate
    instead: one poll at most per `config.heartbeat_interval`, never a
    per-tick sweep.
    """
    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    for node_id in range(10):
        registry.heartbeat(node_id, TrapState.IDLE, at=datetime(2026, 1, 1, tzinfo=UTC))
    gateway = _SpyGateway()
    poll_interval_s = 0.1
    config = StationConfig(heartbeat_interval=timedelta(seconds=poll_interval_s))
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


def test_default_backlog_pacing_holds_the_stations_own_airtime_budget() -> None:
    """docs/protocol/messages.md: a ~50-byte frame costs 0.37 s of airtime
    at Long Fast, and the station's own design budget keeps the channel
    under 1% occupied. CRITICAL 4 measured 30-40 GET_BACKLOG/s under the
    old per-tick full sweep -- 400-1500x over that budget. Pacing one
    GET_BACKLOG poll per `config.heartbeat_interval` (5 minutes,
    `DEFAULT_HEARTBEAT_INTERVAL`) keeps backlog polling's own contribution
    to the budget under 0.2%, regardless of roster size -- unlike a
    per-node interval, which would scale back up with more traps.
    """
    airtime_per_frame_s = 0.37  # docs/protocol/messages.md
    channel_budget_fraction = 0.01  # docs/protocol/messages.md, "under 1% occupied"

    frames_per_second = 1 / DEFAULT_HEARTBEAT_INTERVAL.total_seconds()
    occupancy_fraction = frames_per_second * airtime_per_frame_s

    assert frames_per_second == 1 / 300
    assert occupancy_fraction < channel_budget_fraction
