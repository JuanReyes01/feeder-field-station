"""``domain/pacing.py`` (CRITICAL 3, sdd-verify field-station-p4): the
station-wide token bucket that bounds the *total* transmit rate across
every driver `adapters/workers.py::run_scheduler_loop` calls -- the gap
verify-report-2.md named directly: GET_BACKLOG alone was paced, but
nothing bounded the sum of GET_BACKLOG + command resends + TIME_SYNC, and
the station measured 11.68% real airtime occupancy against a ~1% budget.

Pure state, no clock of its own -- every test here drives ``now`` by hand,
the same discipline ``domain/backlog.py``'s own tests already apply to
``RotationSchedule``.
"""

from __future__ import annotations

import pytest

from feeder_station.domain import pacing


def test_a_fresh_bucket_starts_full_not_empty() -> None:
    """The station's first frame after boot -- one poll, one resend, one
    TIME_SYNC -- must not each wait out a full refill period just because
    the process only just started. See :func:`pacing.start`'s own
    docstring for why: messages.md's budget is about the *sustained*
    rate, not the first instant.
    """
    bucket = pacing.start(capacity=3.0, frames_per_s=0.1, now=0.0)

    _, allowed = pacing.try_consume(bucket, now=0.0)

    assert allowed is True


def test_consuming_more_than_capacity_without_elapsed_time_is_refused() -> None:
    bucket = pacing.start(capacity=2.0, frames_per_s=0.1, now=0.0)

    bucket, first = pacing.try_consume(bucket, now=0.0)
    bucket, second = pacing.try_consume(bucket, now=0.0)
    bucket, third = pacing.try_consume(bucket, now=0.0)

    assert (first, second, third) == (True, True, False)


def test_tokens_refill_at_the_configured_rate_not_faster() -> None:
    """0.1 frames/s -- one token every 10 s. After draining the bucket at
    ``now=0``, 5 s later must still refuse (half a token is not a whole
    one); 10 s later must allow exactly one more send.
    """
    bucket = pacing.start(capacity=1.0, frames_per_s=0.1, now=0.0)
    bucket, _ = pacing.try_consume(bucket, now=0.0)  # drains the starting token

    bucket, too_soon = pacing.try_consume(bucket, now=5.0)
    bucket, on_time = pacing.try_consume(bucket, now=10.0)

    assert too_soon is False
    assert on_time is True


def test_refill_never_exceeds_capacity() -> None:
    """A long idle period must not let the bucket accumulate credit past
    ``capacity`` -- an idle station is not owed a burst proportional to how
    long it stayed idle; messages.md's budget is a *rate*, not a savings
    account.
    """
    bucket = pacing.start(capacity=2.0, frames_per_s=1.0, now=0.0)
    bucket, _ = pacing.try_consume(bucket, now=0.0)
    bucket, _ = pacing.try_consume(bucket, now=0.0)  # drained to 0 at now=0

    bucket, first = pacing.try_consume(bucket, now=1_000_000.0)  # idle for a long time
    bucket, second = pacing.try_consume(bucket, now=1_000_000.0)
    bucket, third = pacing.try_consume(bucket, now=1_000_000.0)

    assert (first, second, third) == (True, True, False)


def test_time_moving_backwards_is_treated_as_no_elapsed_time_not_a_negative_refill() -> None:
    """A caller passing a stale or out-of-order ``now`` (e.g. a monotonic
    clock read racing another thread) must never let the bucket's tokens
    fall below what a same-instant call would have left -- it should look
    like zero time passed, not negative time.
    """
    bucket = pacing.start(capacity=1.0, frames_per_s=0.1, now=100.0)
    bucket, drained = pacing.try_consume(bucket, now=100.0)
    assert drained is True

    bucket, still_refused = pacing.try_consume(bucket, now=50.0)  # now < last_refill_at

    assert still_refused is False


@pytest.mark.parametrize("capacity", [0.0, -1.0])
def test_a_non_positive_capacity_is_rejected(capacity: float) -> None:
    with pytest.raises(ValueError):
        pacing.start(capacity=capacity, frames_per_s=0.1, now=0.0)


@pytest.mark.parametrize("frames_per_s", [0.0, -1.0])
def test_a_non_positive_rate_is_rejected(frames_per_s: float) -> None:
    with pytest.raises(ValueError):
        pacing.start(capacity=1.0, frames_per_s=frames_per_s, now=0.0)
