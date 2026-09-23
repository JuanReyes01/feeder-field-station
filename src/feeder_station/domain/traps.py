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


# `_WIRE_STATE_ORDER = (IDLE, ARMED, FIRED, FAULT)` (adapters/store.py) maps
# each member's *position* directly onto the HEARTBEAT/CAPTURED wire byte
# (WARNING B, sdd-verify field-station-p4 batch 1: `len(TrapState)` derives
# the valid wire range). A fifth member here would silently widen that
# range to accept a byte the protocol has never sent -- which is why
# "unknown" is `Trap.state`'s own type going `TrapState | None` (below),
# not a new `TrapState` value: unknown-to-the-station is not one of the
# four things a trap can report about itself.


@dataclass
class Capture:
    tag: bytes
    at: datetime
    acknowledged: bool = False


@dataclass
class Trap:
    node_id: int
    # WARNING J (sdd-verify field-station-p4, verify-report-batch1-round2.md):
    # `battery_mv`/`targets_version`/`last_seen` all default to `None` --
    # "no data" -- when this station has nothing to report. `state` used to
    # be the one field that invented a value instead (`TrapState.IDLE`, the
    # calmest reading available), so a trap whose only readable report was
    # `FAULT` could render as a calm, healthy "idle" after a corrupt-row
    # skip at boot -- a false all-clear on a trap that may be holding an
    # animal. `None` here means the same thing it means for its neighbours:
    # the station does not know, and must say so, never guess the safest-
    # looking answer.
    state: TrapState | None = None
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

    def captured(self, node_id: int, tag: bytes, state: TrapState, at: datetime) -> Capture:
        """``state`` is the node's own reported ``state_after`` (CRITICAL A,
        sdd-verify field-station-p4 batch 2a), applied the same way
        :meth:`heartbeat` already applies its own ``state`` argument -- a
        CAPTURED is a report about itself (S4), not always ``FIRED``
        (``state_after=0`` means it fired and is idle again). A hardcoded
        ``FIRED`` here is the exact assumption this module's own docstring
        (S4) rules out, and previously disagreed with
        :meth:`restore_reported_state`'s rehydrated answer for the same log.
        """
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.state = state
        trap.last_seen = at
        trap.capture = Capture(tag=tag, at=at)
        return trap.capture

    def restore_capture(self, node_id: int, tag: bytes, at: datetime) -> Capture:
        """Rehydration-only counterpart to :meth:`captured` (CRITICAL 2,
        sdd-verify field-station-p4): a capture replayed from the store at
        boot must never overwrite ``state``/``last_seen`` itself — those
        two facts are decided once, separately, by whichever report wins
        the state race (:meth:`restore_reported_state`, CRITICAL 1 —
        which may be this very same CAPTURED row, or a different one
        entirely), never by the order replay steps happen to run in.

        ``captured()`` is still the one the live ingest path calls
        (``adapters/ingest.py``), where a CAPTURED frame really is the
        trap's own latest report arriving in real time — only a *replayed*
        one, arriving in whatever order the log happens to hold it, must
        not be allowed to regress a fresher report already rehydrated
        first. As of CRITICAL A, ``captured()`` also reads the reported
        ``state`` rather than assuming ``FIRED`` — the two paths agree on
        *what* a CAPTURED means; this method's job stays the narrower *when*
        one may overwrite ``state``/``last_seen``.
        """
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.capture = Capture(tag=tag, at=at)
        return trap.capture

    def restore_reported_state(self, node_id: int, state: TrapState, at: datetime) -> None:
        """Rehydration-only: apply the single winning report (CRITICAL 1,
        sdd-verify field-station-p4 batch 2) — whichever of a node's
        HEARTBEAT, CAPTURED or FAULT rows is most recent by store ``seq``,
        as ``adapters/store.py::rehydrate`` has already decided — setting
        ``state``/``last_seen`` alone, mirroring :meth:`restore_capture`'s
        own "never touch the other one's fields" discipline. Not called at
        all when no state-bearing row could be read (WARNING J): ``Trap.
        state`` then honestly stays at its own default, ``None``, rather
        than this method inventing one.
        """
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.state = state
        trap.last_seen = at

    def restore_heartbeat_fields(
        self, node_id: int, battery_mv: int | None, targets_version: int | None
    ) -> None:
        """Rehydration-only: ``battery_mv``/``targets_version`` exist on no
        report but HEARTBEAT, so they are restored from the latest one
        regardless of whether it wins the state race
        :meth:`restore_reported_state` resolves separately (CRITICAL 1,
        task 2.2's step (b)) — a CAPTURED or FAULT row winning the race
        must not blank out a battery reading the heartbeat still has.
        """
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        if battery_mv is not None:
            trap.battery_mv = battery_mv
        if targets_version is not None:
            trap.targets_version = targets_version

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
                    # WARNING J: `None` here (never a fabricated `"idle"`)
                    # when this station has no readable report to show,
                    # the same "never enabled, never counts as a value"
                    # discipline `battery_mv`/`last_seen` already apply.
                    "state": trap.state.value if trap.state is not None else None,
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
