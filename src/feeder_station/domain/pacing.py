"""Station-wide transmit pacing (CRITICAL 3, sdd-verify field-station-p4):
one shared token bucket, so the station's *total* frame rate -- across
``GET_BACKLOG`` polls, command resends and ``TIME_SYNC`` -- stays under
``docs/protocol/messages.md``'s channel budget, not just whichever single
driver happens to be paced this round. Measured, not assumed: ten armed,
unconfirmed traps drove the station to 11.68% real airtime occupancy
against a ~1% budget with only ``GET_BACKLOG`` paced
(``verify-report-2.md``, CRITICAL 3) -- command resends and ``TIME_SYNC``
each had their own unpaced loop, and nothing bounded the *sum*.

Pure state, no clock of its own -- following ``domain/backlog.py::
RotationSchedule``'s own shape exactly: ``now`` is a plain ``float`` the
caller supplies, never read from a real clock in here, so this module can
be driven by a frozen instant in a test with no special casing.
``adapters/workers.py`` owns the one long-lived instance, mutated under
``registry_lock`` the same way it already owns ``RotationSchedule`` -- and,
per design.md's DD1, this module never imports ``adapters/``, a clock, or
a thread of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# CRITICAL 3: matches the number of transmitters this fix actually found
# (`poll_node`, `send_due_commands`, `attempt_time_sync` -- see this
# batch's apply-progress for the `rg 'write_frame'` sweep that confirmed
# there are exactly three, plus one unrelated, non-paced ACK write in
# `adapters/ingest.py`). One starting token per known category so a normal
# boot's very first legitimate use of each does not have to wait out the
# budget -- the same "burst allowed to settle" allowance
# verify-report-2.md's own measurement methodology already uses.
DEFAULT_CAPACITY = 3.0


@dataclass(frozen=True)
class TokenBucket:
    """``capacity`` tokens at most, refilled continuously at
    ``frames_per_s`` tokens per second. One token is spent per frame the
    station actually transmits; a caller holding no token must not send --
    that one rule, enforced at a single chokepoint (:func:`try_consume`),
    is the whole of this fix.
    """

    capacity: float
    frames_per_s: float
    tokens: float
    last_refill_at: float

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        if self.frames_per_s <= 0:
            raise ValueError("frames_per_s must be positive")


def start(*, capacity: float, frames_per_s: float, now: float) -> TokenBucket:
    """A bucket that starts full, not empty -- see the module docstring:
    the budget is about the *sustained* rate, and the first frame after
    boot must not be penalised for the process having just started.
    """
    return TokenBucket(capacity=capacity, frames_per_s=frames_per_s, tokens=capacity, last_refill_at=now)


def try_consume(bucket: TokenBucket, *, now: float) -> tuple[TokenBucket, bool]:
    """Refill for whatever time has passed since the bucket's last refill,
    then spend one token if one is available.

    Returns the bucket's new state and whether the caller may send. A
    caller that gets ``False`` back must not transmit for this attempt --
    the next opportunity is a later call, never a retry against the same
    ``now``. ``now`` moving backwards (two threads racing a monotonic
    clock read) is treated as zero elapsed time, never a negative refill.
    """
    elapsed = max(0.0, now - bucket.last_refill_at)
    tokens = min(bucket.capacity, bucket.tokens + elapsed * bucket.frames_per_s)
    refilled = replace(bucket, tokens=tokens, last_refill_at=now)
    if refilled.tokens < 1.0:
        return refilled, False
    return replace(refilled, tokens=refilled.tokens - 1.0), True
