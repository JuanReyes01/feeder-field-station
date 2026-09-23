"""domain/clock_trust.py: the station's own clock-trust model, pure.

S12's refusal has exactly one shape on the wire: silence. The protocol's own
`TIME_SYNC` carries no confidence and no refusal field (D7), so
`may_distribute_time()` is the one gate every TIME_SYNC send must pass
through. This file proves the model in isolation from the RTC, NTP and the
wire -- all of that is `adapters/clock.py`'s job.
"""

from __future__ import annotations

from feeder_station.domain.clock_trust import (
    RELEASE_EPOCH_FLOOR,
    ClockTrust,
    epoch_is_above_floor,
    may_distribute_time,
    trust_from_evidence,
)

# --- 9.1: no evidence at all yields unset -----------------------------------


def test_no_rtc_read_and_no_ntp_sync_yields_unset() -> None:
    trust = trust_from_evidence(rtc_epoch=None, ntp_synchronized=False)
    assert trust is ClockTrust.UNSET


def test_a_plausible_rtc_epoch_with_no_ntp_yields_rtc() -> None:
    trust = trust_from_evidence(rtc_epoch=RELEASE_EPOCH_FLOOR + 1, ntp_synchronized=False)
    assert trust is ClockTrust.RTC


def test_ntp_synchronized_yields_ntp_even_with_no_rtc_evidence() -> None:
    trust = trust_from_evidence(rtc_epoch=None, ntp_synchronized=True)
    assert trust is ClockTrust.NTP


# --- 9.3: an unset RTC's own defaults never read as trustworthy ------------


def test_epoch_below_release_floor_is_treated_as_unset() -> None:
    for unset_default in (0, 946_684_800):  # 1970-01-01 and 2000-01-01
        assert not epoch_is_above_floor(unset_default)
        trust = trust_from_evidence(rtc_epoch=unset_default, ntp_synchronized=False)
        assert trust is ClockTrust.UNSET


# --- 9.7/9.9: the S12/D7 gate ------------------------------------------------


def test_may_distribute_time_is_false_while_unset() -> None:
    assert may_distribute_time(ClockTrust.UNSET) is False


def test_may_distribute_time_is_true_once_trusted() -> None:
    assert may_distribute_time(ClockTrust.RTC) is True
    assert may_distribute_time(ClockTrust.NTP) is True
