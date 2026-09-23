"""adapters/clock.py: everything that can fail while establishing clock
trust, and the S12/D7 gate applied at the moment of sending, not at boot.

The one RTC scenario here (`test_rtc_read_failure_yields_unset_not_a_
guessed_time`) is the hardware gate named plainly: whether the physical
DS3231 on the deployed Pi actually survives an unpowered hour (S15's literal
criterion) cannot be proven in CI. This test proves only the failure-mode
discipline -- a fake RTC reader that raises must degrade to `unset`, never
fall back to a guessed system-clock time.
"""

from __future__ import annotations

from datetime import UTC, datetime

from feeder_station.adapters.clock import StationClock, attempt_time_sync
from feeder_station.domain.clock_trust import ClockTrust
from feeder_station.domain.events import STATION_TIME_SYNC_WITHHELD, StationEvent
from feeder_station.domain.frames import decode
from feeder_station.domain.payloads import TIME_SYNC


def _raising_rtc_reader() -> float:
    raise OSError("no such device")


# --- 9.5/9.6: a failed RTC read degrades to unset, never a guessed time ----


def test_rtc_read_failure_yields_unset_not_a_guessed_time() -> None:
    clock = StationClock(read_rtc_epoch=_raising_rtc_reader, ntp_synchronized=lambda: False)

    assert clock.trust() is ClockTrust.UNSET


def test_a_valid_rtc_read_with_no_ntp_yields_rtc() -> None:
    clock = StationClock(read_rtc_epoch=lambda: 1_800_000_000.0, ntp_synchronized=lambda: False)

    assert clock.trust() is ClockTrust.RTC


def test_ntp_synchronized_yields_ntp() -> None:
    clock = StationClock(read_rtc_epoch=_raising_rtc_reader, ntp_synchronized=lambda: True)

    assert clock.trust() is ClockTrust.NTP


# --- 9.7: TIME_SYNC scheduling is gated at the moment of sending -----------


class _FixedClock:
    """A `Clock` double that reports a fixed trust and fixed time -- the
    adapter-level scheduling tests don't need a real RTC or NTP probe,
    only `domain/clock_trust.py`'s model already proven in isolation.
    """

    def __init__(self, trust: ClockTrust, at: datetime) -> None:
        self._trust = trust
        self._at = at

    def now(self) -> datetime:
        return self._at

    def trust(self) -> ClockTrust:
        return self._trust


class _SpyStore:
    def __init__(self) -> None:
        self.appended: list[StationEvent] = []

    def append(self, event: StationEvent, *, received_epoch: float, clock_trust: str) -> None:
        self.appended.append(event)


class _SpyGateway:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write_frame(self, raw_frame: bytes) -> bool:
        self.written.append(raw_frame)
        return True


def test_time_sync_scheduling_skips_a_node_while_unset() -> None:
    """The scenario spec.md names: while the clock is unset, no TIME_SYNC
    frame is sent, and a withheld-sync entry is recorded -- the only shape
    S12's refusal can take, since the protocol has no refusal frame (D7).
    """
    clock = _FixedClock(ClockTrust.UNSET, datetime(2026, 1, 1, tzinfo=UTC))
    store = _SpyStore()
    gateway = _SpyGateway()

    sent = attempt_time_sync(9, clock=clock, store=store, gateway=gateway)

    assert sent is False
    assert gateway.written == []
    assert len(store.appended) == 1
    withheld = store.appended[0]
    assert withheld.source == "station"
    assert withheld.node_id == 9
    assert withheld.msg_type == STATION_TIME_SYNC_WITHHELD


# --- 9.9: TIME_SYNC resumes once the clock is trusted -----------------------


class _MutableTrustClock:
    """A `Clock` double whose trust can change between calls -- the fixed
    `_FixedClock` above cannot express the actual transition
    clock-trust/spec.md's scenario names.
    """

    def __init__(self, trust: ClockTrust, at: datetime) -> None:
        self.trust_value = trust
        self._at = at

    def now(self) -> datetime:
        return self._at

    def trust(self) -> ClockTrust:
        return self.trust_value


def test_time_sync_resumes_once_trusted() -> None:
    """clock-trust/spec.md, "TIME_SYNC resumes once trusted": "GIVEN the
    station's clock state transitions from `unset` to `rtc` or `ntp`, WHEN
    a node is next due for `TIME_SYNC`, THEN the frame is sent." Before
    this fix (sdd-verify, field-station-p4), this test built a clock that
    was already `RTC` and called `attempt_time_sync` once -- no `unset`
    state and no transition existed anywhere in it; it was
    indistinguishable from `test_a_valid_rtc_read_with_no_ntp_yields_rtc`
    plus a send. This drives the actual transition: withheld while unset,
    then sent once the same clock becomes trusted.
    """
    at = datetime(2026, 1, 1, tzinfo=UTC)
    clock = _MutableTrustClock(ClockTrust.UNSET, at)
    store = _SpyStore()
    gateway = _SpyGateway()

    withheld = attempt_time_sync(9, clock=clock, store=store, gateway=gateway)
    assert withheld is False
    assert gateway.written == []
    assert len(store.appended) == 1
    assert store.appended[0].msg_type == STATION_TIME_SYNC_WITHHELD

    clock.trust_value = ClockTrust.RTC
    sent = attempt_time_sync(9, clock=clock, store=store, gateway=gateway)

    assert sent is True
    assert len(store.appended) == 1  # no second withheld row once trusted
    assert len(gateway.written) == 1
    frame = decode(gateway.written[0])
    assert frame.msg_type == TIME_SYNC
    assert frame.node_id == 9
    epoch = (
        frame.payload[0]
        | (frame.payload[1] << 8)
        | (frame.payload[2] << 16)
        | (frame.payload[3] << 24)
    )
    assert epoch == int(at.timestamp())
