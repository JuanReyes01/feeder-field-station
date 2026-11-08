"""Pending downlink commands: what the station is asking a trap to become,
answered only by what the trap itself reports (S4).

**DD15 — the gap this module closes.** Before this module existed,
``Registry.heartbeat()`` cleared a bare ``arm_pending: bool`` only when the
reported state was ``ARMED``. That rule has no way to express a pending
*disarm*: a disarmed trap never reports ``ARMED``, so nothing would ever
clear it, and a disarm command would stay pending forever, silently.
``Command.satisfied_by(...)`` replaces the single boolean with a per-kind
rule: an ``ARM`` is answered by the trap reporting armed, a ``DISARM`` by
the trap reporting *not* armed, and a ``SET_TARGETS`` by the trap's
reported ``targets_version`` matching the version this command was issued
for.

This module deliberately never imports ``domain.traps``: ``Trap`` holds a
``Command`` (so ``traps.py`` imports this module), and ``satisfied_by``
only ever needs the one bit of a heartbeat it actually cares about --
``ReportedState.armed`` -- not the whole ``TrapState`` enum. Reducing the
fact to a bare ``bool`` here is what keeps this module a leaf with no
import cycle back to ``traps.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum

# design.md section 4 sets this shape ("sleep 1, 2, 4, 8, 16, 30... seconds
# capped at 30") for the serial gateway's own reconnect loop
# (adapters/serial_gateway.py::BACKOFF_SCHEDULE_S). A pending command's
# resend used to copy that same 30 s cap verbatim -- this is its own copy,
# not an import of that adapter: domain/ never imports adapters/ (DD1) --
# but a command's own confirmation is a different fact than a reconnect's:
# it can only ever be cleared by the trap's own next HEARTBEAT (DD15), so
# capping the resend below the trap's own heartbeat cadence (S11's default,
# `config.DEFAULT_HEARTBEAT_INTERVAL`, 300 s) cannot buy a faster
# confirmation -- only more unconfirmable retransmits, each one competing
# with GET_BACKLOG and TIME_SYNC for the same shared airtime budget
# (CRITICAL 3, sdd-verify field-station-p4). Raised toward that cadence.
RESEND_BACKOFF_S: tuple[float, ...] = (
    1.0,
    2.0,
    4.0,
    8.0,
    16.0,
    32.0,
    64.0,
    128.0,
    256.0,
    300.0,
)

# A target list is what a trap hunts with, so it is never left waiting minutes
# between resends: once the trap is back in range it gets the list within this.
SET_TARGETS_MAX_RESEND_S = 30.0


class CommandKind(str, Enum):
    ARM = "arm"
    DISARM = "disarm"
    SET_TARGETS = "set_targets"


# docs/pending.md, "A command the node refuses is retried forever": the
# node's own FAULT.code values are not numbered by messages.md at all
# ("does not number them", feeder-node-firmware's own
# components/station_proto/station_proto.h comment on STATION_FAULT_*) --
# this project only has one FAULT code today that names a specific
# *command* as refused, rather than reporting a hardware condition
# (a stalled servo, a dead SD card): STATION_FAULT_ARM_REQUIRES_PRESENCE
# (5). The node cannot arm itself -- a person has to place the cord over
# the mechanism -- so every ARM downlink is answered this way, never
# obeyed and never merely delayed.
#
# This mapping is therefore only as reliable as that one firmware-only
# convention, not a protocol guarantee: a future fault code that similarly
# names a refused command (a DISARM or SET_TARGETS this station cannot
# satisfy) extends this dict, not this module's shape. See
# `domain/traps.py::Registry.fault()` for how a name here is turned into a
# cancelled `pending_command` -- and why an unrecognised code is the
# conservative case (nothing cancelled), not the reverse.
FAULT_CODE_REFUSES: dict[int, CommandKind] = {
    5: CommandKind.ARM,  # STATION_FAULT_ARM_REQUIRES_PRESENCE
    # STATION_FAULT_ARM_INTERRUPTED: the board reset while arming. Resending
    # ARM would only reset it again, so the operator decides when to retry.
    10: CommandKind.ARM,
}


@dataclass(frozen=True)
class ReportedState:
    """The one slice of a HEARTBEAT/HELLO a pending command is ever checked
    against (S4) -- never the whole payload.
    """

    armed: bool
    targets_version: int | None = None


@dataclass(frozen=True)
class Command:
    """A downlink the station is asking a trap to apply, until the trap's
    own report says it has been -- never asserted as delivered just from
    having sent the frame (S4).

    ``targets_version`` only means something for ``SET_TARGETS``; ``ARM``
    and ``DISARM`` leave it ``None``. ``attempts``/``last_sent_at`` drive
    ``due_for_resend`` and are advanced only by ``sent()``.
    """

    kind: CommandKind
    targets_version: int | None = None
    attempts: int = 0
    last_sent_at: datetime | None = None
    first_sent_at: datetime | None = None

    def satisfied_by(self, heartbeat: ReportedState) -> bool:
        if self.kind is CommandKind.ARM:
            return heartbeat.armed
        if self.kind is CommandKind.DISARM:
            return not heartbeat.armed
        return heartbeat.targets_version == self.targets_version

    def due_for_resend(self, now: datetime) -> bool:
        """Whether this still-pending command should be sent again right
        now: immediately if it has never been sent, otherwise once the
        capped backoff delay for its attempt count has elapsed since the
        last send.
        """
        if self.last_sent_at is None:
            return True
        index = min(self.attempts - 1, len(RESEND_BACKOFF_S) - 1)
        delay = RESEND_BACKOFF_S[index]
        if self.kind is CommandKind.SET_TARGETS:
            delay = min(delay, SET_TARGETS_MAX_RESEND_S)
        return now - self.last_sent_at >= timedelta(seconds=delay)

    def sent(self, at: datetime) -> Command:
        """A new value recording that this command was just (re)sent."""
        return replace(
            self,
            attempts=self.attempts + 1,
            last_sent_at=at,
            first_sent_at=self.first_sent_at or at,
        )
