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
from feeder_station.config import StationConfig
from feeder_station.domain import payloads
from feeder_station.domain.clock_trust import ClockTrust
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

    run_gateway_loop(
        gateway,  # type: ignore[arg-type]
        store=store,
        registry=registry,
        registry_lock=threading.Lock(),
        publish=published.append,
        clock=_FixedClock(),
        stop=stop,
    )

    assert stop.is_set()
    assert registry.trap(3).state is TrapState.ARMED  # decoded from the fixture payload
    assert published  # step 9 of the S9 ordering ran
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
