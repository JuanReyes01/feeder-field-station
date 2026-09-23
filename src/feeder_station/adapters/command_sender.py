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
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from feeder_station.domain import events, frames, payloads
from feeder_station.domain.commands import Command, CommandKind
from feeder_station.domain.frames import Frame
from feeder_station.domain.traps import Registry, Trap

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


def _encode_single_frame(command: Command, *, now: datetime, arm_duration_s: float) -> bytes | None:
    if command.kind is CommandKind.ARM:
        expires_epoch = int(now.timestamp() + arm_duration_s)
        return payloads.encode_arm(expires_epoch=expires_epoch)
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
    trap: Trap, command: Command, *, store: CommandStore, gateway: CommandGateway, now: datetime
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
    results = [gateway.write_frame(raw) for raw in wire_frames]
    if not all(results):
        return False

    trap.pending_command = command.sent(now)
    return True


def send_due_commands(
    registry: Registry,
    *,
    store: CommandStore,
    gateway: CommandGateway,
    now: datetime,
    arm_duration_s: float,
    budget: TransmitBudget,
) -> list[int]:
    """One pass over every trap with a pending command due for resend.

    Returns the node ids a frame was genuinely written for, in roster
    order. A command's own ``sent()`` (slice 8) -- which advances the
    capped backoff ``due_for_resend()`` reads next time -- is only called
    when the write(s) to the gateway actually succeeded; a dropped write
    must not look, to the backoff schedule, like a send that happened.

    **CRITICAL 3 (sdd-verify field-station-p4):** ``budget`` is checked
    once per trap actually due, *inside* this loop -- not once for the
    whole call -- because a single sweep can cover every trap in the
    roster in one pass (verify-report-2.md measured ten traps, all
    unconfirmed, driving 11.68% real airtime occupancy this way). A trap
    refused a token stays pending, unattempted, and due again on a later
    sweep; it is never dropped.
    """
    sent_to: list[int] = []
    for trap in registry.traps():
        command = trap.pending_command
        if command is None or not command.due_for_resend(now):
            continue
        if not budget.try_send():
            continue

        if command.kind is CommandKind.SET_TARGETS:
            if _send_set_targets(trap, command, store=store, gateway=gateway, now=now):
                sent_to.append(trap.node_id)
            continue

        msg_type = _WIRE_MSG_TYPE.get(command.kind)
        payload = _encode_single_frame(command, now=now, arm_duration_s=arm_duration_s)
        if msg_type is None or payload is None:
            continue

        frame = Frame(msg_type=msg_type, node_id=trap.node_id, payload=payload)
        if gateway.write_frame(frames.encode(frame)):
            trap.pending_command = command.sent(now)
            sent_to.append(trap.node_id)

    return sent_to
