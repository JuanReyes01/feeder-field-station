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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from . import targets_reassembly
from .commands import FAULT_CODE_REFUSES, RESEND_BACKOFF_S, Command, CommandKind, ReportedState
from .targets_reassembly import ReassemblyState

# How many times one operator answer to a capture sends RESOLVE_CAPTURE.
RESOLVE_CAPTURE_SENDS = 2


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


@dataclass(frozen=True)
class PendingTargetsFetch:
    """A station-initiated `GET_TARGETS` request, outstanding until a
    `TARGETS` reply completes and resolves the mismatch that raised it
    (`Registry._note_targets_mismatch`).

    Deliberately its own type, never `Command`/`CommandKind`, and never
    stored in `Trap.pending_command`. `Command` tracks an *operator's*
    intent -- the trap is asked to *become* something, and is checked with
    `satisfied_by` against a `ReportedState` -- and `Registry._set_pending`
    lets a second command supersede the first on purpose (an operator
    changed their mind). A `GET_TARGETS` fetch is not an operator's intent
    at all: it is the station asking a question, answered only by a
    `TARGETS` uplink, and it must never share that slot -- an automatic
    fetch silently cancelling an operator's own pending `ARM`/`DISARM`/
    `SET_TARGETS` (or the reverse) would be exactly the kind of quiet
    state loss this project's own commands module already guards against
    for operator commands.

    ``attempts``/``last_sent_at`` reuse `commands.RESEND_BACKOFF_S`'s own
    schedule -- the same reasoning applies unchanged: capped below the
    trap's own heartbeat cadence buys nothing, only unconfirmable
    retransmits competing with every other downlink for the same shared
    airtime budget.
    """

    attempts: int = 0
    last_sent_at: datetime | None = None

    def due_for_resend(self, now: datetime) -> bool:
        if self.last_sent_at is None:
            return True
        index = min(self.attempts - 1, len(RESEND_BACKOFF_S) - 1)
        return now - self.last_sent_at >= timedelta(seconds=RESEND_BACKOFF_S[index])

    def sent(self, at: datetime) -> PendingTargetsFetch:
        return replace(self, attempts=self.attempts + 1, last_sent_at=at)


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
    # WARNING C (sdd-verify field-station-p4 batch 2, verify-report-batch2a.md):
    # WARNING J's own residual. `state` above can be a real, readable
    # report and still not be the node's *newest* one -- when a fresher
    # state-bearing row exists but could not be read, the older, readable
    # report still wins the state race (a real report beats nothing at
    # all) but must say it might not be current, the same "unknown beats a
    # default that renders as safe" rule WARNING J already applies when
    # there is no fact at all. Cleared by the next live report (`heartbeat`/
    # `captured`/`fault`), since that report is by definition the newest
    # thing the station has heard.
    state_may_be_stale: bool = False
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
    # station/remote-arm-restored: the honest caveat this station can
    # actually afford. Firmware's `wall_reached()` is still a placeholder
    # (docs/pending.md, "Nothing knows whether the arm actually reached
    # the wall") -- a HEARTBEAT reporting `armed` after a remote `ARM`
    # confirms the commanded sweep ran, never that the cord reached the
    # mechanism. This station cannot verify the hardware, so it records
    # the one fact it actually has instead: when an operator last asked
    # this trap to arm, remotely. Same independence as `desired_tags`
    # above -- what the *station* asked for, never cleared by a
    # heartbeat (S4 governs `state`, not this) -- so the caveat survives
    # exactly into the moment (`state == ARMED`) an operator needs it
    # most, rather than disappearing the instant the command confirms.
    armed_via_remote_command_at: datetime | None = None
    # GET_TARGETS/TARGETS (docs/protocol/messages.md, added 2026-09-27): what
    # the trap itself last reported holding, entirely separate from
    # `desired_tags` above -- the station's intent and the trap's own report
    # are different facts that can legitimately disagree, which is the whole
    # reason this pair of messages exists. Never written by anything that
    # touches `desired_tags`, and vice versa -- see `Registry.
    # reported_targets_chunk`/`restore_reported_targets` (this field) versus
    # `command_set_targets`/`restore_desired_tags` (`desired_tags`).
    reported_targets_version: int | None = None
    reported_tags: tuple[bytes, ...] | None = None
    # In-flight `TARGETS` reassembly (`domain/targets_reassembly.py`) -- pure,
    # transient, never persisted directly: losing an *incomplete* generation
    # on a station restart is safe (a fresh `GET_TARGETS` simply asks again),
    # unlike `reported_tags`/`reported_targets_version` above, which are the
    # last *completed* answer and are rehydrated (`adapters/store.py::Store.
    # reported_targets`).
    targets_reassembly: ReassemblyState | None = None
    # Outstanding automatic `GET_TARGETS` request, armed and cleared by
    # `Registry._note_targets_mismatch` alone -- see `PendingTargetsFetch`'s
    # own docstring for why this cannot share `pending_command`'s slot.
    pending_targets_fetch: PendingTargetsFetch | None = None
    # A test fire from the station is DISARM followed by ARM: the trap goes to
    # the released angle and back, without recording a capture. Set by the
    # test-fire route, cleared once the ARM is queued (app.py).
    rearm_after_test_fire: bool = False
    # RESOLVE_CAPTURE (0x88) frames still to send: the operator answered the
    # capture banner, so the trap forgets the tag it last fired on and that
    # bird can fire it again on its next visit. Set by
    # `Registry.acknowledge`, counted down by
    # `adapters/command_sender.py::send_due_commands`. Nothing answers it, so
    # it goes out RESOLVE_CAPTURE_SENDS times; a repeat is harmless.
    resolve_capture_sends_left: int = 0

    def is_reachable(self, now: datetime, silence_after: timedelta | None = None) -> bool:
        if self.last_seen is None:
            return False
        window = silence_after if silence_after is not None else timedelta(minutes=5)
        return now - self.last_seen <= window


def _targets_match(trap: Trap) -> bool | None:
    """Whether what was asked for and what the trap reported actually agree.

    Three answers, deliberately: ``None`` for "we cannot say", ``False`` for
    "we can, and they disagree", ``True`` only for a comparison that means
    something.

    The `None` cases are the point. The obvious one is no `TARGETS` reply yet.
    The other one cost a bug: `targets_version` 0 is the node saying it has
    **never received `SET_TARGETS` at all** -- `components/target_list`'s own
    meaning for that value, carried unchanged into the reply. The firmware's
    encoding is explicit that an empty list and "never received" are
    wire-identical (`count` 0) and are told apart *only* by `targets_version`.

    A station's own desired list can also be empty, after a fresh install or a
    wiped database. Compared by content those two agree, so a plain set
    comparison answers `True` -- "verified in agreement" about a trap that has
    never heard of this station. That is precisely the defect
    `GET_TARGETS` exists to expose, so it must not be what `GET_TARGETS`
    reports. There is nothing to agree with, so the answer is `None`.

    Wanting a tag while the node holds nothing is a different matter: that is
    knowably wrong whether or not the node was ever configured, so it stays
    `False`.
    """
    if trap.reported_tags is None:
        return None
    if trap.reported_targets_version == 0 and not trap.desired_tags:
        return None
    return set(trap.desired_tags or ()) == set(trap.reported_tags)


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
        trap.state_may_be_stale = False  # WARNING C: a live report is always the newest one
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

        # GET_TARGETS trigger (docs/protocol/messages.md): re-evaluate on
        # every heartbeat, using whatever `targets_version` this trap now
        # holds -- unconditionally, so a heartbeat that could not supply a
        # fresh reading (the defensive `targets_version=None` fallback,
        # `adapters/ingest.py::_apply_heartbeat`) still re-checks against
        # the value already on file, rather than skipping the check.
        self._note_targets_mismatch(trap)

        return trap

    def command_arm(self, node_id: int, at: datetime) -> None:
        self._set_pending(node_id, Command(kind=CommandKind.ARM))
        # station/remote-arm-restored: recorded regardless of whether this
        # call came from a live `POST /arm` or a rehydrated legacy
        # `OPERATOR_ARM` row (`adapters/store.py::_rehydrate_desired_
        # command`) -- both are equally "an operator asked this trap to
        # arm, remotely", which is the fact `Trap.armed_via_remote_
        # command_at`'s own docstring names.
        self._traps[node_id].armed_via_remote_command_at = at

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

        BEHAVIOUR 4 / SUGGESTION 5 (verify-report-batch2d.md): also checks
        a pending command against ``state_after``, the same DD15 rule
        :meth:`heartbeat` already runs -- ``state_after`` is a genuinely
        reported fact, the same standing as a HEARTBEAT's ``state``.
        Before this fix, ``captured()`` never ran ``satisfied_by`` at all,
        while ``adapters/store.py``'s rehydration path already did (from
        the very same fact) -- the same log gave two different answers
        depending on whether the station had restarted. Deliberately not
        mirrored in :meth:`fault`, which carries no such reported fact --
        see that method's own docstring.
        """
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.state = state
        trap.state_may_be_stale = False  # WARNING C: a live report is always the newest one
        trap.last_seen = at
        trap.capture = Capture(tag=tag, at=at)
        if trap.pending_command is not None:
            report = ReportedState(
                armed=state is TrapState.ARMED, targets_version=trap.targets_version
            )
            if trap.pending_command.satisfied_by(report):
                trap.pending_command = None
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

    def mark_state_possibly_stale(self, node_id: int) -> None:
        """Rehydration-only (WARNING C, sdd-verify field-station-p4 batch 2,
        verify-report-batch2a.md): called when a node's state-bearing rows
        were a mix of readable and unreadable -- :meth:`restore_reported_state`
        already applied the best available fact, but a fresher, unreadable
        sibling row means that fact might not be the node's true latest.
        Never called from the live ingest path: a live report is by
        definition the newest thing the station has heard, so
        :meth:`heartbeat`/:meth:`captured`/:meth:`fault` each clear this
        instead of setting it.
        """
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.state_may_be_stale = True

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

    def reported_targets_chunk(
        self,
        node_id: int,
        *,
        targets_version: int,
        seq: int,
        seq_total: int,
        tags: Sequence[bytes],
    ) -> tuple[bytes, ...] | None:
        """One chunk of a live `TARGETS` uplink (docs/protocol/messages.md):
        mirrors `command_set_targets`'s own downlink chunking, reversed.
        Folded into `Trap.targets_reassembly`
        (`domain/targets_reassembly.py::accept_chunk`, a pure fold) and
        only becomes `reported_tags`/`reported_targets_version` -- the
        fact `GET /api/traps` renders and `_note_targets_mismatch` checks
        -- once every part has arrived, the same "applied only once every
        part has arrived" rule `SET_TARGETS` already follows in the other
        direction. A chunk for a *different* `targets_version` than the
        one already in progress discards that in-progress reassembly
        outright (see `accept_chunk`'s own docstring for why) rather than
        merging across two generations.

        Returns the completed tag list when this chunk finishes a
        generation, so the caller (`adapters/ingest.py`) knows to persist
        it (`adapters/store.py::Store.record_reported_targets`) -- never
        ``None`` mistaken for "nothing changed" when a caller needs to
        durably record a fresh answer.
        """
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.targets_reassembly, completed = targets_reassembly.accept_chunk(
            trap.targets_reassembly,
            targets_version=targets_version,
            seq=seq,
            seq_total=seq_total,
            tags=tags,
        )
        if completed is not None:
            trap.reported_targets_version = targets_version
            trap.reported_tags = completed
            self._note_targets_mismatch(trap)
        return completed

    def restore_reported_targets(
        self, node_id: int, *, targets_version: int | None, tags: Sequence[bytes] | None
    ) -> None:
        """Rehydration-only: apply the trap's last fully-reassembled
        `TARGETS` reply directly. `adapters/store.py::Store.
        reported_targets` already read this back as one row from
        `node_cursor`'s own cache (SCHEMA_VERSION 4) -- there is no
        reassembly to replay here, unlike the live per-chunk path
        (`reported_targets_chunk`).

        ``targets_version``/``tags`` are ``None`` -- rather than this
        method being skipped outright -- for a node with no completed
        `TARGETS` reply on file at all. Skipping the call entirely was the
        bug this signature closes: `_note_targets_mismatch` below never ran
        for that node, so a `GET_TARGETS` fetch that would have been armed
        on the live path (a rehydrated `HEARTBEAT` already gave it a real
        `targets_version`, and the honest "never reported" default is
        `None`, which disagrees with it) stayed un-armed after a restart
        instead -- the live and rehydrated registries silently diverging on
        `targets_fetch_pending`.
        """
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.reported_targets_version = targets_version
        trap.reported_tags = None if tags is None else tuple(tags)
        self._note_targets_mismatch(trap)

    def _note_targets_mismatch(self, trap: Trap) -> None:
        """The GET_TARGETS trigger (docs/protocol/messages.md): a fetch is
        armed whenever `reported_targets_version` disagrees with what this
        trap's own `targets_version` (from its latest `HEARTBEAT`) says --
        which covers both trigger conditions at once: no reported list at
        all yet (`reported_targets_version is None` while `targets_version`
        is a real value) and a stale one (the two disagree on a number).
        Both `None` -- a trap this station has never even heard a
        heartbeat from -- reads as "nothing to check yet", not a mismatch:
        there is no known real version to try to satisfy.

        Never a periodic poll: this is only ever (re-)armed from a
        `HEARTBEAT` actually reporting a new mismatch
        (:meth:`heartbeat`), or from a `TARGETS` reply changing
        `reported_targets_version` itself (:meth:`reported_targets_chunk`,
        :meth:`restore_reported_targets`) -- never from a timer of its
        own.

        A fetch already pending for this same mismatch is left exactly as
        it is -- `attempts`/`last_sent_at`'s own backoff, not a fresh
        reset -- so a trap that keeps re-heartbeating the same
        disagreement every interval does not have its resend backoff
        clobbered back to "send immediately" on every beat.
        """
        if trap.reported_targets_version == trap.targets_version:
            trap.pending_targets_fetch = None
            return
        if trap.pending_targets_fetch is None:
            trap.pending_targets_fetch = PendingTargetsFetch()

    def fault(self, node_id: int, at: datetime, code: int | None) -> None:
        """A `FAULT` uplink (radio-gateway spec.md): the trap itself reported
        it, exactly the same standing as a heartbeat (S4) — not this
        station's own guess about a node gone quiet, which `is_reachable()`
        computes separately.

        BEHAVIOUR 4 / SUGGESTION 5 (verify-report-batch2d.md): unlike
        :meth:`heartbeat`/:meth:`captured`, this deliberately does NOT run
        `satisfied_by` against a pending command. A `FAULT` payload
        (`domain/payloads.py::decode_fault`) carries no state field at all
        -- only `code`/`detail` -- so `state = TrapState.FAULT` here is
        this module's own convention for display, not a reported fact.
        Treating "something is wrong" as "not armed" would be exactly the
        assumption this module's own S4 docstring rules out: a faulted
        trap may still be physically armed.

        docs/pending.md, "A command the node refuses is retried forever":
        `satisfied_by` is the wrong tool for a *refusal* anyway -- it asks
        "does the trap's state now match what we asked for", never "did
        the trap explicitly say no". `code` answers the second question,
        via `commands.FAULT_CODE_REFUSES`: when it names the same kind as
        `pending_command`, the node has said, in this project's one
        firmware-assigned vocabulary for it, that this command will never
        be satisfied -- so it is cancelled outright, not left to resend
        until a budget or an operator notices. `code=None`, or any code
        this mapping has no entry for, is the conservative case: nothing
        is cancelled, and the command stays pending exactly as it did
        before this fix -- messages.md itself does not number `FAULT.code`
        at all, so a code this station cannot name is not evidence of
        anything, in either direction.
        """
        trap = self._traps.setdefault(node_id, Trap(node_id=node_id))
        trap.state = TrapState.FAULT
        trap.state_may_be_stale = False  # WARNING C: a live report is always the newest one
        trap.last_seen = at
        if (
            trap.pending_command is not None
            and code is not None
            and FAULT_CODE_REFUSES.get(code) is trap.pending_command.kind
        ):
            trap.pending_command = None

    def acknowledge(self, node_id: int, at: datetime) -> None:
        trap = self._traps[node_id]
        trap.capture = None
        trap.resolve_capture_sends_left = RESOLVE_CAPTURE_SENDS

    def cancel_command(self, node_id: int, at: datetime) -> None:
        """docs/pending.md, "There is no way to cancel a pending command":
        even with :meth:`fault`'s named-refusal cancellation above, a
        command the node never answers at all -- not confirmed, not
        refused -- has no way out short of this. Unconditional, unlike
        :meth:`fault`'s check against `FAULT_CODE_REFUSES`: an operator may
        cancel a command for any reason (a misconfigured target list, a
        command issued by mistake), not only one the node has explicitly
        refused. Same shape as :meth:`acknowledge` -- `at` is accepted for
        symmetry with the other operator-command methods and because
        `app.py`'s own durable event row already carries the timestamp;
        the registry itself has nothing dated to store for a cancellation.
        """
        self._traps[node_id].pending_command = None

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
                    # WARNING C: a real, readable report can still not be
                    # the node's newest one -- true only while an unread
                    # sibling row might have been fresher (rehydration);
                    # cleared by the node's next live report.
                    "state_may_be_stale": trap.state_may_be_stale,
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
                    # GET_TARGETS/TARGETS (docs/protocol/messages.md): what
                    # the trap itself last reported holding -- `None` (never
                    # a fabricated `[]`) when this station has no accepted
                    # `TARGETS` reply for it yet, the same "never enabled,
                    # never counts as a value" discipline `battery_mv`/
                    # `last_seen` already apply, so an operator can tell "we
                    # don't know" from "the trap says it has nothing".
                    "reported_targets": (
                        None
                        if trap.reported_tags is None
                        else [tag.hex().upper() for tag in trap.reported_tags]
                    ),
                    "reported_targets_version": trap.reported_targets_version,
                    # Whether the two facts above actually agree, compared by
                    # content (a set, not wire order) -- `None` for "we don't
                    # know yet" (no `TARGETS` reply), never conflated with
                    # `False` ("we know, and they disagree"). This is the
                    # divergence `targets`/`reported_targets` alone cannot
                    # show without an operator comparing two arrays by eye.
                    "targets_match": _targets_match(trap),
                    # Whether an automatic GET_TARGETS is currently
                    # outstanding for this trap -- explains why
                    # `reported_targets` might still be stale or absent.
                    "targets_fetch_pending": trap.pending_targets_fetch is not None,
                    "last_seen": trap.last_seen.isoformat() if trap.last_seen else None,
                    "test_fire_pending": trap.rearm_after_test_fire,
                    "resolve_capture_pending": trap.resolve_capture_sends_left > 0,
                    # station/remote-arm-restored: the honest caveat --
                    # `wall_reached()` is still a firmware placeholder
                    # (docs/pending.md), so `state == "armed"` only ever
                    # confirms the commanded sweep ran, never that the
                    # mechanism is primed. Never cleared by a heartbeat,
                    # same independence as `targets` above -- what an
                    # operator last asked for, not what the trap reports.
                    "armed_via_remote_command_at": (
                        trap.armed_via_remote_command_at.isoformat()
                        if trap.armed_via_remote_command_at
                        else None
                    ),
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
