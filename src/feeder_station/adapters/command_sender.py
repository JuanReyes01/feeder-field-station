"""Wires ``domain/traps.py``'s pending-``Command`` model (DD15, slice 8)
into real downlink frames: the last of slice 12's four orphaned drivers,
``Command.due_for_resend()``/``sent()``, had a fully tested contract and no
caller until this module.

**ARM and DISARM are sent for real.** ``SET_TARGETS`` is deliberately
**not** sent from here, on purpose, every time: ``domain/traps.py::Command``
only ever carries a ``targets_version`` (slice 8's own model), never the
tag bytes an operator actually typed. Nothing before slice 13 persists
those bytes anywhere (see this change's slice-11 apply-progress notes, and
slice 13's own task list) — encoding a ``SET_TARGETS`` frame here would
have to either resend stale bytes from some earlier session or send an
*empty* target list, which is not "nothing to send yet", it is a real
instruction that silently clears whatever a trap is actually hunting. A
command left untouched stays ``due_for_resend()``-true, so slice 13's own
sender picks it up the moment it has real bytes to send — deferring
honestly beats guessing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from feeder_station.domain import frames, payloads
from feeder_station.domain.commands import Command, CommandKind
from feeder_station.domain.frames import Frame
from feeder_station.domain.traps import Registry

# What each `CommandKind` this module actually sends becomes on the wire.
# `SET_TARGETS` is intentionally absent — see the module docstring above.
_WIRE_MSG_TYPE: dict[CommandKind, int] = {
    CommandKind.ARM: payloads.ARM,
    CommandKind.DISARM: payloads.DISARM,
}


class CommandGateway(Protocol):
    """The one call this module needs from ``SerialGateway``."""

    def write_frame(self, raw_frame: bytes) -> bool: ...


def _encode(command: Command, *, now: datetime, arm_duration_s: float) -> bytes | None:
    if command.kind is CommandKind.ARM:
        expires_epoch = int(now.timestamp() + arm_duration_s)
        return payloads.encode_arm(expires_epoch=expires_epoch)
    if command.kind is CommandKind.DISARM:
        return payloads.encode_disarm()
    return None  # SET_TARGETS -- see module docstring.


def send_due_commands(
    registry: Registry, *, gateway: CommandGateway, now: datetime, arm_duration_s: float
) -> list[int]:
    """One pass over every trap with a pending command due for resend.

    Returns the node ids a frame was genuinely written for, in roster
    order. A command's own ``sent()`` (slice 8) — which advances the
    capped backoff ``due_for_resend()`` reads next time — is only called
    when ``gateway.write_frame()`` itself reports success; a dropped write
    must not look, to the backoff schedule, like a send that happened.
    """
    sent_to: list[int] = []
    for trap in registry.traps():
        command = trap.pending_command
        if command is None or not command.due_for_resend(now):
            continue

        msg_type = _WIRE_MSG_TYPE.get(command.kind)
        payload = _encode(command, now=now, arm_duration_s=arm_duration_s)
        if msg_type is None or payload is None:
            continue

        frame = Frame(msg_type=msg_type, node_id=trap.node_id, payload=payload)
        if gateway.write_frame(frames.encode(frame)):
            trap.pending_command = command.sent(now)
            sent_to.append(trap.node_id)

    return sent_to
