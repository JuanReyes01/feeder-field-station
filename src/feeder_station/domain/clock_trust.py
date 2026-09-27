"""The station's own clock-trust model: `unset` / `rtc` / `ntp`, and the
S12/D7 gate that withholds `TIME_SYNC` while it is `unset` (DD14).

The station is the time authority for every node -- GPS was removed from the
traps precisely because the station was made responsible for handing out the
time instead. A Raspberry Pi carries no real-time clock of its own and
normally recovers it from the internet, which at this site is intermittent
(docs/requirements.md, "Two risks that this document exists to catch"). A
power cut that coincides with an outage is exactly the failure that shows up
only in the field, never on the bench, because either ordinary event alone
is harmless.

Three layers close that gap, and this module is the last of them:

1. The DS3231 (S15) carries time across a power cut.
2. NTP corrects the RTC whenever the internet happens to be up.
3. **This module is the floor underneath both.** If neither has ever set the
   clock, the station is `unset`, and `may_distribute_time()` says so --
   which is the only form S12's refusal can take, because the protocol's own
   `TIME_SYNC` frame carries no confidence and no refusal field (D7,
   `domain/payloads.py::encode_time_sync`). A node due for a sync simply
   sees silence where one would have been; the station's own record says
   why (`STATION_TIME_SYNC_WITHHELD`, `domain/events.py`).

Pure by construction (DD1): every fact this module reasons about -- an RTC
epoch, whether NTP is disciplined -- is handed in by the caller.
`adapters/clock.py` is the one place that actually reads a clock.
"""

from __future__ import annotations

from enum import Enum


class ClockTrust(str, Enum):
    """Exactly one of these at any moment (spec.md, "Clock trust states")."""

    UNSET = "unset"
    RTC = "rtc"
    NTP = "ntp"


# design.md section 5: "the system clock is below an epoch floor baked in at
# release -- a time before this software existed cannot be real, and 1970
# and 2000-01-01 are exactly what an unset RTC reports." Both of those
# defaults must read as untrustworthy, so 2000-01-01 alone will not do as
# the floor; this is fixed at the start of the year this change shipped
# (2026-09-23) and never moves with the wall clock.
RELEASE_EPOCH_FLOOR = 1_735_689_600  # 2025-01-01T00:00:00Z


def epoch_is_above_floor(epoch: float) -> bool:
    """Whether `epoch` is plausibly real, not an unset RTC's own default."""
    return epoch >= RELEASE_EPOCH_FLOOR


def trust_from_evidence(*, rtc_epoch: float | None, ntp_synchronized: bool) -> ClockTrust:
    """Combine what the adapter observed into exactly one `ClockTrust`.

    `rtc_epoch` is `None` whenever `adapters/clock.py` could not read the
    RTC at all (missing device, I/O error, never set) -- that failure
    degrades straight to `unset`, never to a guessed time (S15). An epoch
    that does read back but sits at or below `RELEASE_EPOCH_FLOOR` gets the
    identical treatment, because that is exactly what a dead or never-set
    RTC reports.

    NTP wins outright when synchronized: it disciplines the system clock
    directly, so a synchronized NTP daemon is trusted regardless of what the
    RTC separately reports.
    """
    if ntp_synchronized:
        return ClockTrust.NTP
    if rtc_epoch is not None and epoch_is_above_floor(rtc_epoch):
        return ClockTrust.RTC
    return ClockTrust.UNSET


def may_distribute_time(trust: ClockTrust) -> bool:
    """S12/D7: the one requirement this whole module exists to enforce."""
    return trust is not ClockTrust.UNSET


# HEARTBEAT's own `clock_source` byte (docs/protocol/messages.md: "clock_source:
# 0 unset, 1 RTC") is a *node's* report about its *own* clock -- a different
# fact from `ClockTrust` above, which is the *station's*. Named here, next to
# it, so a bare `1` never appears unexplained at its one reader
# (`adapters/workers.py`'s TIME_SYNC retry gate, the clock-trust bug fix:
# see that module's own docstring for what trusting a *sent* attempt instead
# of this reported fact cost).
NODE_CLOCK_SOURCE_RTC = 1


def node_clock_is_trusted(clock_source: int | None) -> bool:
    """Whether the node's own last-reported ``clock_source`` already means
    a trustworthy clock -- ``None`` (no HEARTBEAT read yet) is untrusted,
    the same "no fact yet is not the safest-looking guess" rule
    ``domain/traps.py::Trap`` already applies to every other optional field
    it has not heard about yet.
    """
    return clock_source == NODE_CLOCK_SOURCE_RTC
