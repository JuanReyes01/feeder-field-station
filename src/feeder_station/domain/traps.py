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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from .commands import Command, CommandKind, ReportedState


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
    pending_command: Command | None = None
    capture: Capture | None = None
    # S2/13.11-13.12: the tag bytes an operator last submitted for this
    # trap, independent of whether that `SET_TARGETS` is still pending or
    # already confirmed -- "what a trap is actually hunting" per this
    # slice's own task list, not only "what is not yet delivered". Set by
    # `command_set_targets`; never cleared by a heartbeat, unlike
    # `pending_command` (S4: reported state, never assumed, does not apply
    # here -- this is what the *station* is asking for, not what a trap
    # has reported).
    desired_tags: tuple[bytes, ...] | None = None

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

        # The trap has now told us what it is, so any command we were waiting
        # on is checked against that report (DD15) -- ARM, DISARM, and
        # SET_TARGETS each have their own satisfied_by rule; there is no
        # single shared "clear on ARMED" rule that could quietly leave a
        # pending disarm stuck forever.
        if trap.pending_command is not None:
            report = ReportedState(
                armed=state is TrapState.ARMED, targets_version=trap.targets_version
            )
            if trap.pending_command.satisfied_by(report):
                trap.pending_command = None

        return trap

    def command_arm(self, node_id: int, at: datetime) -> None:
        self._set_pending(node_id, Command(kind=CommandKind.ARM))

    def command_disarm(self, node_id: int, at: datetime) -> None:
        self._set_pending(node_id, Command(kind=CommandKind.DISARM))

    def command_set_targets(
        self,
        node_id: int,
        targets_version: int,
        at: datetime,
        tags: Sequence[bytes] = (),
    ) -> None:
        """``tags`` defaults to empty for backward compatibility with
        callers that only ever exercised the version-reconciliation half of
        this model (slice 8/9's own tests) -- ``app.py``'s route (slice 13)
        is the one real caller that has actual bytes to pass.
        """
        self._set_pending(
            node_id, Command(kind=CommandKind.SET_TARGETS, targets_version=targets_version)
        )
        self._traps[node_id].desired_tags = tuple(tags)

    def _set_pending(self, node_id: int, command: Command) -> None:
        """Shared by every `command_*` method: create the trap on first
        contact, same as `heartbeat()` and `captured()` already do, and
        install the new pending command, replacing whatever was pending
        before (an operator issuing a second command supersedes the first,
        never queues behind it).
        """
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.pending_command = command

    def captured(self, node_id: int, tag: bytes, at: datetime) -> Capture:
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.state = TrapState.FIRED
        trap.last_seen = at
        trap.capture = Capture(tag=tag, at=at)
        return trap.capture

    def restore_capture(self, node_id: int, tag: bytes, at: datetime) -> Capture:
        """Rehydration-only counterpart to :meth:`captured` (CRITICAL 2,
        sdd-verify field-station-p4): a capture replayed from the store at
        boot must never overwrite ``state``/``last_seen`` with the
        capture's own — possibly hours-old — timestamp. Those two facts are
        the heartbeat replay's business alone (S4, trap-commands/spec.md:
        "The trap's displayed state MUST always be whatever its own most
        recent HEARTBEAT or HELLO reported").

        ``captured()`` itself is unchanged and still used as-is by the live
        ingest path (``adapters/ingest.py``), where a CAPTURED frame really
        is the trap's own latest report arriving in real time — only a
        *replayed* one, arriving in whatever order the log happens to hold
        it, must not be allowed to regress a fresher heartbeat already
        rehydrated first.
        """
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.capture = Capture(tag=tag, at=at)
        return trap.capture

    def restore_desired_tags(self, node_id: int, tags: Sequence[bytes]) -> None:
        """Rehydration-only: replay the latest ``OPERATOR_SET_TARGETS``
        bytes into ``desired_tags`` on their own, independent of whether
        that edit is still pending or already confirmed — the same
        independence :meth:`command_set_targets` already gives this field
        during live operation (never cleared by a heartbeat, S2/13.11-13.12).
        """
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.desired_tags = tuple(tags)

    def fault(self, node_id: int, at: datetime) -> None:
        """A `FAULT` uplink (radio-gateway spec.md): the trap itself reported
        it, exactly the same standing as a heartbeat (S4) — not this
        station's own guess about a node gone quiet, which `is_reachable()`
        computes separately.
        """
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.state = TrapState.FAULT
        trap.last_seen = at

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
                    "pending_command": (
                        trap.pending_command.kind.value if trap.pending_command else None
                    ),
                    "desired_targets_version": (
                        trap.pending_command.targets_version
                        if trap.pending_command is not None
                        and trap.pending_command.kind is CommandKind.SET_TARGETS
                        else None
                    ),
                    # 13.11/13.12: what the station is asking this trap to
                    # hunt, not only whether an edit is still pending --
                    # uppercase to match `capture.tag`'s own hex rendering.
                    "targets": (
                        [tag.hex().upper() for tag in trap.desired_tags]
                        if trap.desired_tags
                        else []
                    ),
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
