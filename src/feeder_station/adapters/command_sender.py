"""Wires ``domain/traps.py``'s pending-``Command`` model (DD15, slice 8)
into real downlink frames: the last of slice 12's four orphaned drivers,
``Command.due_for_resend()``/``sent()``, had a fully tested contract and no
caller until this module.

**ARM, DISARM and SET_TARGETS are all sent for real, as of slice 13.**
Before this slice, ``SET_TARGETS`` was deliberately withheld here:
``domain/traps.py::Command`` only ever carries a ``targets_version`` (slice
8's own model), never the tag bytes an operator actually typed, and nothing
persisted those bytes anywhere. Slice 13 closes that gap on the other end:
``app.py``'s ``PUT /targets`` route now writes the operator's exact tag
list to the store (``OPERATOR_SET_TARGETS``, ``domain/events.py``), so this
module can finally read them back through :func:`_stored_tags` and encode a
real frame instead of guessing at one.

**What is proven here, and what plainly is not.** This module's own tests,
and ``tests/domain/test_traps.py``'s existing S2 scenarios, prove the
frames are byte-correct -- chunked exactly as
``domain/payloads.py::encode_set_targets`` (slice 2) specifies -- and that
a trap's pending command clears only once its own ``HEARTBEAT`` reports the
matching ``targets_version`` (S4/DD15), never from merely having sent a
frame. What none of that proves, and what this docstring says plainly
instead of leaving implied: **no real trap has ever received one.** Real
delivery needs the firmware's own ``station_link`` component, which does
not exist anywhere in this repository. Until it does, every ``SET_TARGETS``
frame this module writes is correct-and-unconfirmed, never
correct-and-delivered -- exactly the same honest distance separately named
for the serial gateway (slice 5) and the deployment unit (slice 12): a
passing test here proves the station's own half of the wire, not the other
end of it.

**`GET_TARGETS` (0x87), added alongside `TARGETS` (0x07, docs/protocol/
messages.md).** A second, unrelated kind of pending fact this module now
dispatches: `Trap.pending_targets_fetch`, armed and cleared entirely by
`Registry._note_targets_mismatch`, never by an operator action -- see
`PendingTargetsFetch`'s own docstring (`domain/traps.py`) for why it
cannot share `pending_command`'s slot. `_send_get_targets` mirrors the
single-frame ARM/DISARM path above, not `_send_set_targets`'s chunked
one: the request itself is always one empty-payload frame, and it is the
*answer* that may need reassembling, on the way back in.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from feeder_station.domain import events, frames, payloads
from feeder_station.domain.commands import Command, CommandKind
from feeder_station.domain.frames import Frame
from feeder_station.domain.traps import PendingTargetsFetch, Registry, Trap

# What each single-frame `CommandKind` this module sends becomes on the
# wire. `SET_TARGETS` is handled separately by `_send_set_targets` below --
# it is the one kind that can span more than one frame, and the one kind
# whose bytes come from the store rather than from the command itself.
_WIRE_MSG_TYPE: dict[CommandKind, int] = {
    CommandKind.ARM: payloads.ARM,
    CommandKind.DISARM: payloads.DISARM,
}


class CommandGateway(Protocol):
    """The one call this module needs from ``SerialGateway``."""

    def write_frame(self, raw_frame: bytes) -> bool: ...


class TransmitBudget(Protocol):
    """The one call this module needs from ``adapters/workers.py``'s
    shared station-wide pacer (CRITICAL 3, sdd-verify field-station-p4).

    Required, not optional -- WARNING 13's own lesson, applied here: a
    defaultable parameter that silently changes behaviour under test (or
    in production, for a future caller that forgets it) is the wrong
    default. A caller with no budget object to hand it must construct one,
    not skip the check.
    """

    def try_send(self) -> bool: ...

    def try_send_n(self, n: int) -> bool:
        """BEHAVIOUR 1 (verify-report-final.md, sdd-verify
        field-station-p4): an atomic charge of ``n`` tokens for one
        multi-frame send -- ``_send_set_targets`` below is the one caller,
        because it is the one kind of command that can produce more than
        one frame. All ``n`` or none; see :func:`domain.pacing.try_consume_n`
        for why a partial charge is never correct here.
        """
        ...


class _FieldsRow(Protocol):
    """The one attribute this module needs off a stored row --
    ``adapters/store.py::StoredEvent`` satisfies this structurally, and a
    test can fake it with nothing heavier than a two-field double (DD16's
    own "Protocol fake" discipline, applied one layer up the stack).

    A read-only ``@property`` rather than a plain attribute: ``StoredEvent``
    is a frozen dataclass, so mypy treats its ``fields`` as a read-only
    attribute -- a plain Protocol attribute is assumed settable and would
    reject it as an incompatible match.
    """

    @property
    def fields(self) -> dict[str, object]: ...


class CommandStore(Protocol):
    """The one call this module needs from ``adapters/store.py::Store``:
    reading back the exact tag bytes an operator submitted for a pending
    ``SET_TARGETS`` -- ``domain/traps.py::Command`` itself carries only the
    version number (slice 8's own model), never the bytes.
    """

    def latest(self, node_id: int, msg_type: int) -> _FieldsRow | None: ...


def _encode_single_frame(command: Command) -> bytes | None:
    if command.kind is CommandKind.ARM:
        # Position model (docs/protocol/messages.md): ARM is a command to
        # drive the servo to the armed position, always obeyed, never a
        # state transition the node may accept or refuse -- so there is no
        # verdict left on the node side for a deadline to be judged
        # against. `expires_epoch` stays in the wire format (retired, not
        # removed: renumbering or repurposing a pinned field is exactly
        # what this protocol's own fault-code registry refuses to do) and
        # is always `0`, messages.md's own "no deadline on the order".
        return payloads.encode_arm(expires_epoch=0)
    if command.kind is CommandKind.DISARM:
        return payloads.encode_disarm()
    return None  # SET_TARGETS is handled by `_send_set_targets`, never here.


def _stored_tags(store: CommandStore, node_id: int, targets_version: int) -> list[bytes] | None:
    """The exact bytes an operator submitted for ``targets_version``, read
    back from the store row ``app.py``'s ``PUT /targets`` route wrote.

    Returns ``None`` -- nothing durable to send yet -- when no row exists
    at all, or when the most recently stored row is for a *different*
    version: a fresher edit has already superseded the one this
    ``Command`` was issued for, and ``Registry.command_set_targets`` would
    already have replaced ``pending_command`` with one carrying that newer
    version on its own next call, so this is a narrow defensive read, not
    the expected path.
    """
    row = store.latest(node_id, events.OPERATOR_SET_TARGETS)
    if row is None:
        return None
    if row.fields.get("targets_version") != targets_version:
        return None
    tags = row.fields.get("tags")
    if not isinstance(tags, list):
        return None
    return [bytes.fromhex(tag) for tag in tags if isinstance(tag, str)]


def _send_set_targets(
    trap: Trap,
    command: Command,
    *,
    store: CommandStore,
    gateway: CommandGateway,
    now: datetime,
    budget: TransmitBudget,
) -> bool:
    """Encode and write every chunk
    ``domain/payloads.py::encode_set_targets`` (slice 2) produces for this
    command's ``targets_version``.

    A split list is sent as one unit: every chunk is attempted, and
    ``sent()`` only runs when every write succeeded. trap-commands
    spec.md's own delivery model never tracks which half of a split list
    arrived -- only whether the trap's later ``HEARTBEAT`` reports the
    matching version (S2) -- so there is nothing to gain from marking a
    partial write as sent, and a real risk in doing so: it would make the
    backoff schedule believe a chunk that never left the station did.

    **BEHAVIOUR 1 (verify-report-final.md, sdd-verify field-station-p4):**
    the budget charge is ``len(wire_frames)`` tokens, spent atomically via
    ``budget.try_send_n`` *before* any frame is written -- never the single
    per-trap ``try_send()`` the caller used to spend regardless of how many
    chunks this command actually produced, which let one operator action
    put an N-chunk list on the wire for the price of one frame. A refusal
    here spends nothing and writes nothing: the trap stays pending, exactly
    as the single-frame path below already behaves when its own
    ``try_send()`` is refused.
    """
    assert command.targets_version is not None  # only None for ARM/DISARM (domain/commands.py)
    tags = _stored_tags(store, trap.node_id, command.targets_version)
    if tags is None:
        return False

    wire_frames = [
        frames.encode(Frame(msg_type=payloads.SET_TARGETS, node_id=trap.node_id, payload=payload))
        for payload in payloads.encode_set_targets(
            targets_version=command.targets_version, tags=tags
        )
    ]
    if not budget.try_send_n(len(wire_frames)):
        return False

    results = [gateway.write_frame(raw) for raw in wire_frames]
    if not all(results):
        return False

    trap.pending_command = command.sent(now)
    return True


def _send_get_targets(
    trap: Trap,
    fetch: PendingTargetsFetch,
    *,
    gateway: CommandGateway,
    now: datetime,
    budget: TransmitBudget,
) -> bool:
    """One `GET_TARGETS` frame (docs/protocol/messages.md) -- always a
    single, empty-payload frame, never chunked (that is `TARGETS`'s own
    answer's problem, reassembled on the way back in
    `Registry.reported_targets_chunk`), so this mirrors the ARM/DISARM
    single-frame path above, not ``_send_set_targets``'s multi-chunk one.
    """
    if not budget.try_send():
        return False

    frame = Frame(
        msg_type=payloads.GET_TARGETS, node_id=trap.node_id, payload=payloads.encode_get_targets()
    )
    if not gateway.write_frame(frames.encode(frame)):
        return False

    trap.pending_targets_fetch = fetch.sent(now)
    return True


def _send_resolve_capture(
    trap: Trap,
    *,
    gateway: CommandGateway,
    budget: TransmitBudget,
) -> bool:
    """One `RESOLVE_CAPTURE` frame (docs/protocol/messages.md): the operator
    answered the capture, so the trap forgets the tag it last fired on.
    Empty payload, no answer, so there is nothing to confirm -- the count
    only goes down on a write that actually happened, the same rule
    ``_send_get_targets`` follows for its own backoff.
    """
    if not budget.try_send():
        return False

    frame = Frame(
        msg_type=payloads.RESOLVE_CAPTURE,
        node_id=trap.node_id,
        payload=payloads.encode_resolve_capture(),
    )
    if not gateway.write_frame(frames.encode(frame)):
        return False

    trap.resolve_capture_sends_left -= 1
    return True


def send_due_commands(
    registry: Registry,
    *,
    store: CommandStore,
    gateway: CommandGateway,
    now: datetime,
    budget: TransmitBudget,
) -> list[int]:
    """One pass over every trap with a pending command, or an outstanding
    `GET_TARGETS` fetch, due for resend.

    Returns the node ids a frame was genuinely written for, in roster
    order. A command's own ``sent()`` (slice 8) -- which advances the
    capped backoff ``due_for_resend()`` reads next time -- is only called
    when the write(s) to the gateway actually succeeded; a dropped write
    must not look, to the backoff schedule, like a send that happened.
    ``PendingTargetsFetch.sent()`` follows the identical rule for its own
    backoff.

    **CRITICAL 3 (sdd-verify field-station-p4):** ``budget`` is checked
    once per trap actually due, *inside* this loop -- not once for the
    whole call -- because a single sweep can cover every trap in the
    roster in one pass (verify-report-2.md measured ten traps, all
    unconfirmed, driving 11.68% real airtime occupancy this way). A trap
    refused a token stays pending, unattempted, and due again on a later
    sweep; it is never dropped.

    **BEHAVIOUR 1 (verify-report-final.md, sdd-verify field-station-p4):**
    that "once per trap" check is a *single* token only for the
    single-frame kinds below (``ARM``/``DISARM``/``GET_TARGETS``) --
    ``SET_TARGETS`` can produce more than one frame per trap, so its own
    charge is made inside ``_send_set_targets`` itself, sized to the
    chunk count, not made here as a flat one-token gate before dispatch.

    **A trap's pending command and its pending `GET_TARGETS` fetch are
    dispatched independently**, in the same pass: `Trap.pending_command`
    (an operator's intent) and `Trap.pending_targets_fetch` (the
    station's own automatic reconciliation, `Registry.
    _note_targets_mismatch`) are unrelated facts that may both be due, one,
    or neither, for the same trap at the same tick -- see
    `PendingTargetsFetch`'s own docstring for why they never share a slot.
    """
    sent_to: list[int] = []
    for trap in registry.traps():
        sent = False

        command = trap.pending_command
        if command is not None and command.due_for_resend(now):
            if command.kind is CommandKind.SET_TARGETS:
                if _send_set_targets(
                    trap, command, store=store, gateway=gateway, now=now, budget=budget
                ):
                    sent = True
            elif budget.try_send():
                msg_type = _WIRE_MSG_TYPE.get(command.kind)
                payload = _encode_single_frame(command)
                if msg_type is not None and payload is not None:
                    frame = Frame(msg_type=msg_type, node_id=trap.node_id, payload=payload)
                    if gateway.write_frame(frames.encode(frame)):
                        trap.pending_command = command.sent(now)
                        sent = True

        fetch = trap.pending_targets_fetch
        if (
            fetch is not None
            and fetch.due_for_resend(now)
            and _send_get_targets(trap, fetch, gateway=gateway, now=now, budget=budget)
        ):
            sent = True

        if trap.resolve_capture_sends_left > 0 and _send_resolve_capture(
            trap, gateway=gateway, budget=budget
        ):
            sent = True

        if sent:
            sent_to.append(trap.node_id)

    return sent_to
