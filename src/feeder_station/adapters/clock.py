"""Everything that can fail while establishing the station's clock trust
(design.md section 5, DD14): reading the DS3231 RTC, probing whether the
system clock is NTP-disciplined, and gating every `TIME_SYNC` send on the
answer. `domain/clock_trust.py` stays pure and never touches any of it.

**Hardware gate, named plainly (S15):** whether the physical DS3231 on the
deployed Pi actually holds time across an unpowered hour cannot be proven
here or in CI. What this module's own tests prove is narrower and
deliberate: that a *failed* RTC read degrades to `unset`, never to a
guessed time -- see `tests/adapters/test_clock.py::
test_rtc_read_failure_yields_unset_not_a_guessed_time`, which drives a fake
RTC reader that raises, not a real DS3231. See `docs/hardware/clock.md` for
which probe below has actually been verified against a real image, and
which is still an open question.
"""

from __future__ import annotations

import ctypes
import fcntl
import struct
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from feeder_station.domain.clock_trust import ClockTrust, may_distribute_time, trust_from_evidence
from feeder_station.domain.events import STATION_TIME_SYNC_WITHHELD, StationEvent
from feeder_station.domain.frames import Frame, encode
from feeder_station.domain.payloads import TIME_SYNC, encode_time_sync


class Clock(Protocol):
    """design.md's own interface sketch (section "Interfaces")."""

    def now(self) -> datetime: ...
    def trust(self) -> ClockTrust: ...


# --- RTC read: Linux's RTC_RD_TIME ioctl, never exercised against real ------
# --- hardware by any test here -- that is exactly S15's own gate. ----------

_RTC_TIME_FIELDS = "9i"  # struct rtc_time: 9 C ints (sec..isdst), <linux/rtc.h>
_RTC_TIME_SIZE = struct.calcsize(_RTC_TIME_FIELDS)
# RTC_RD_TIME = _IOR('p', 0x09, struct rtc_time) -- computed once, not
# re-derived at import time from the kernel headers, the same "documented
# assumption" discipline domain/payloads.py already applies to unpinned
# wire widths.
RTC_RD_TIME = 0x80247009


def _default_read_rtc_epoch(device: Path = Path("/dev/rtc0")) -> float:
    """Read the DS3231 through the kernel's RTC driver.

    Any failure -- the device missing, permission denied, a short or
    malformed ioctl response -- is left to raise `OSError` (or a subclass
    of it); the caller, `StationClock._rtc_epoch_or_none`, is the one place
    that turns that into "no evidence" rather than a guessed time.
    """
    buf = bytearray(_RTC_TIME_SIZE)
    with open(device, "rb") as handle:
        fcntl.ioctl(handle, RTC_RD_TIME, buf)
    tm_sec, tm_min, tm_hour, tm_mday, tm_mon, tm_year, _wday, _yday, _isdst = struct.unpack(
        _RTC_TIME_FIELDS, bytes(buf)
    )
    # struct rtc_time follows struct tm's own conventions: tm_year counts
    # years since 1900, tm_mon is 0-indexed.
    return datetime(
        tm_year + 1900, tm_mon + 1, tm_mday, tm_hour, tm_min, tm_sec, tzinfo=UTC
    ).timestamp()


# --- NTP probe: adjtimex's STA_UNSYNC bit, with a timedatectl fallback -----


class _Timex(ctypes.Structure):
    """The subset of Linux's `struct timex` (`<sys/timex.h>`) this probe
    needs. Only `status` is read; every other field exists purely to keep
    the struct's memory layout correct for the syscall.
    """

    _fields_ = [
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time_sec", ctypes.c_long),
        ("time_usec", ctypes.c_long),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("_padding", ctypes.c_int * 11),
    ]


STA_UNSYNC = 0x0040  # <sys/timex.h>


def _adjtimex_synchronized() -> bool | None:
    """`True`/`False` from the kernel's own `STA_UNSYNC` bit, or `None` if
    the syscall itself could not be made. design.md section 5 names this
    the primary probe precisely because it is image-agnostic: chrony, ntpd
    and systemd-timesyncd all maintain this bit, so the station never has
    to guess which one an operator's image happens to run.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        buf = _Timex()
        result = libc.adjtimex(ctypes.byref(buf))
    except (OSError, AttributeError):
        return None
    if result < 0:
        return None
    return not bool(buf.status & STA_UNSYNC)


def _timedatectl_synchronized() -> bool | None:
    """Fallback probe (task 9.11): `timedatectl show -p NTPSynchronized` is
    systemd's own equivalent question, asked the polling way instead of the
    syscall way, for an image where `adjtimex` cannot be called at all.
    """
    try:
        result = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() == "yes"


def _default_ntp_synchronized() -> bool:
    """`adjtimex` first; `timedatectl` only when that probe could not even
    be made. Either probe answering "not synchronized" is a real answer,
    not a failure -- only both being *inconclusive* degrades to `False`,
    the same "never guess" discipline the RTC read follows.
    """
    probed = _adjtimex_synchronized()
    if probed is not None:
        return probed
    fallback = _timedatectl_synchronized()
    return bool(fallback)


def _system_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class StationClock:
    """The real `Clock`: reads the DS3231 and probes NTP synchronization on
    every call -- design.md section 5 is explicit that the trust check
    happens "at the moment of sending, not at boot", so nothing here is
    cached across calls.

    Both probes are injected (the same discipline `adapters/serial_gateway.
    py` already applies to `open_port`/`sleep`), so every scenario in
    `tests/adapters/test_clock.py` runs against a fake -- including the one
    scenario that can honestly be proven about the RTC in CI: that a failed
    read degrades to `unset`, never to a guessed time.
    """

    read_rtc_epoch: Callable[[], float] = _default_read_rtc_epoch
    ntp_synchronized: Callable[[], bool] = _default_ntp_synchronized
    system_now: Callable[[], datetime] = field(default=_system_now)

    def now(self) -> datetime:
        return self.system_now()

    def trust(self) -> ClockTrust:
        return trust_from_evidence(
            rtc_epoch=self._rtc_epoch_or_none(), ntp_synchronized=self.ntp_synchronized()
        )

    def _rtc_epoch_or_none(self) -> float | None:
        try:
            return self.read_rtc_epoch()
        except OSError:
            # S15: any RTC failure is "no evidence", never a guessed time.
            return None


# --- The command sender: the one call site allowed to emit TIME_SYNC -------


class TimeSyncStore(Protocol):
    """The one call this module needs from `adapters/store.py::Store`."""

    def append(self, event: StationEvent, *, received_epoch: float, clock_trust: str) -> object: ...


class TimeSyncPort(Protocol):
    """The one call this module needs from `SerialGateway`."""

    def write_frame(self, raw_frame: bytes) -> bool: ...


def attempt_time_sync(
    node_id: int, *, clock: Clock, store: TimeSyncStore, gateway: TimeSyncPort
) -> bool:
    """design.md section 5, verbatim: "the check lives ... at the moment of
    sending, not at boot and not in unit ordering." Every scheduled
    `TIME_SYNC` must go through this function, and only this function, so
    that rule cannot be bypassed by a caller that checks trust once and
    sends later.

    Returns whether the frame was actually sent. When it is not, this is
    the whole of S12's refusal: D7 means `TIME_SYNC` carries no confidence
    and no refusal field, so the node just sees silence where a sync would
    have been -- the station's own record says exactly why, via a
    `STATION_TIME_SYNC_WITHHELD` row written through `store.append()`
    (design.md, DD2: an operator action or the station's own witnessed fact
    is a row in the same log as everything a node reports).
    """
    trust = clock.trust()
    now = clock.now()

    if not may_distribute_time(trust):
        withheld = StationEvent(
            source="station",
            node_id=node_id,
            msg_type=STATION_TIME_SYNC_WITHHELD,
            msg_id=None,
            node_epoch=None,
            tag=None,
            fields={"reason": "clock_unset"},
            payload=None,
        )
        store.append(withheld, received_epoch=now.timestamp(), clock_trust=trust.value)
        return False

    frame = Frame(
        msg_type=TIME_SYNC, node_id=node_id, payload=encode_time_sync(epoch=int(now.timestamp()))
    )
    return gateway.write_frame(encode(frame))
