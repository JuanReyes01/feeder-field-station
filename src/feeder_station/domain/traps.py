"""What the station believes about each trap.

Two rules shape this module, both from the requirements:

**S4 — state is reported, never assumed.** A trap's state is whatever its own
heartbeat last said. Commanding it to arm sets a *pending* flag and nothing
else, so the interface can never show an intention as a fact.

**S3 — a capture is a state, not an event.** A trap that fires stays in an
unacknowledged capture until a person clears it. Anyone who opens the interface
afterwards sees it, whether they were watching at the time or not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any


class TrapState(str, Enum):
    IDLE = "idle"
    ARMED = "armed"
    FIRED = "fired"
    FAULT = "fault"


@dataclass
class Capture:
    tag: bytes
    at: datetime
    acknowledged: bool = False


@dataclass
class Trap:
    node_id: int
    state: TrapState = TrapState.IDLE
    battery_mv: int | None = None
    targets_version: int | None = None
    last_seen: datetime | None = None
    arm_pending: bool = False
    capture: Capture | None = None

    def is_reachable(self, now: datetime, silence_after: timedelta | None = None) -> bool:
        if self.last_seen is None:
            return False
        window = silence_after if silence_after is not None else timedelta(minutes=5)
        return now - self.last_seen <= window


class Registry:
    """The traps the station has heard from, and what it knows about them."""

    def __init__(self, silence_after: timedelta) -> None:
        self._silence_after = silence_after
        self._traps: dict[int, Trap] = {}

    def trap(self, node_id: int) -> Trap:
        return self._traps[node_id]

    def traps(self) -> list[Trap]:
        """Ordered by node id, so the interface never reshuffles under a finger."""
        return [self._traps[node_id] for node_id in sorted(self._traps)]

    def heartbeat(
        self,
        node_id: int,
        state: TrapState,
        at: datetime,
        battery_mv: int | None = None,
        targets_version: int | None = None,
    ) -> Trap:
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.state = state
        trap.last_seen = at
        if battery_mv is not None:
            trap.battery_mv = battery_mv
        if targets_version is not None:
            trap.targets_version = targets_version

        # The trap has now told us what it is, so any command we were waiting on
        # is answered — whichever way it went.
        if state is TrapState.ARMED:
            trap.arm_pending = False

        return trap

    def command_arm(self, node_id: int, at: datetime) -> None:
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.arm_pending = True

    def captured(self, node_id: int, tag: bytes, at: datetime) -> Capture:
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.state = TrapState.FIRED
        trap.last_seen = at
        trap.capture = Capture(tag=tag, at=at)
        return trap.capture

    def acknowledge(self, node_id: int, at: datetime) -> None:
        self._traps[node_id].capture = None

    def unacknowledged_captures(self, now: datetime) -> list[Trap]:
        return [t for t in self.traps() if t.capture is not None and not t.capture.acknowledged]

    def view(self, now: datetime) -> list[dict[str, Any]]:
        """What the interface renders.

        `trustworthy` is the honest bit: an armed trap we cannot reach is still
        reported as armed, because that is what it last said, but nobody should
        act as though it can catch anything.
        """
        rows: list[dict[str, Any]] = []
        for trap in self.traps():
            reachable = trap.is_reachable(now, self._silence_after)
            rows.append(
                {
                    "node_id": trap.node_id,
                    "state": trap.state.value,
                    "reachable": reachable,
                    "trustworthy": reachable,
                    "battery_mv": trap.battery_mv,
                    "targets_version": trap.targets_version,
                    "arm_pending": trap.arm_pending,
                    "last_seen": trap.last_seen.isoformat() if trap.last_seen else None,
                    "capture": (
                        {
                            "tag": trap.capture.tag.hex().upper(),
                            "at": trap.capture.at.isoformat(),
                        }
                        if trap.capture
                        else None
                    ),
                }
            )
        return rows
