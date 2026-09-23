"""The shape of a stored row, and how a decoded frame becomes one.

``StationEvent`` is what ``adapters/store.py`` persists (design.md, section
2): the bytes and the reading of them (DD4), before any clock stamp or trust
level is attached — those are the store's business, decided at the moment of
writing, not here. This module stays pure: no clock read, no database, no
socket.

Node frames are not the only thing this station ever records. An operator
arming a trap, acknowledging a capture, or the station itself withholding a
``TIME_SYNC`` (D7) are also rows in the same log (design.md, DD2) — that is
why ``StationEvent.source`` exists. Wire ``msg_type`` values are a single
byte (0-255, see ``domain.frames``); the ``OPERATOR_*``/``STATION_*``
constants below start above that range on purpose, so a ``(node_id,
msg_type)`` pair is never ambiguous between a node's own wire vocabulary and
the station's internal bookkeeping of what an operator did.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from . import payloads
from .frames import Frame

Source = Literal["node", "operator", "station"]

# Station-internal msg_type values for source != "node" events. Deliberately
# outside the wire's 0-255 byte range (domain.frames.HEADER_BYTES's
# msg_type field is one byte) so these can never collide with a real
# uplink/downlink message type.
OPERATOR_ARM = 0x100
OPERATOR_DISARM = 0x101
OPERATOR_SET_TARGETS = 0x102
OPERATOR_CAPTURE_ACKNOWLEDGED = 0x103
STATION_TIME_SYNC_WITHHELD = 0x200


@dataclass(frozen=True)
class StationEvent:
    source: Source
    node_id: int
    msg_type: int
    msg_id: int | None  # None unless source == "node" (D5: the wire value, not a key)
    node_epoch: int | None
    tag: bytes | None
    fields: Mapping[str, object]
    payload: bytes | None


class UninterpretableFrame(ValueError):
    """A frame whose ``msg_type`` has no known uplink payload decoder."""


_DECODERS: Mapping[int, Callable[[bytes], object]] = {
    payloads.HELLO: payloads.decode_hello,
    payloads.HEARTBEAT: payloads.decode_heartbeat,
    payloads.VISIT: payloads.decode_visit,
    payloads.CAPTURED: payloads.decode_captured,
    payloads.FAULT: payloads.decode_fault,
    payloads.ACK: payloads.decode_ack,
}


def interpret(frame: Frame) -> StationEvent:
    """Turn a decoded uplink frame into the row-shape the store persists.

    Always ``source="node"`` — this function's whole job is turning
    something a node said into a stored row. Events with any other source
    (an operator's command, the station's own withheld ``TIME_SYNC``) are
    constructed directly by their callers, since nothing on the wire
    produces them.
    """
    decoder = _DECODERS.get(frame.msg_type)
    if decoder is None:
        raise UninterpretableFrame(f"no uplink decoder for msg_type {frame.msg_type:#04x}")

    decoded = decoder(frame.payload)
    return StationEvent(
        source="node",
        node_id=frame.node_id,
        msg_type=frame.msg_type,
        msg_id=frame.msg_id,
        node_epoch=_node_epoch_of(decoded),
        tag=_tag_of(decoded),
        fields=_fields_of(decoded),
        payload=frame.payload,
    )


def _node_epoch_of(decoded: object) -> int | None:
    if isinstance(decoded, payloads.Heartbeat):
        return decoded.clock_epoch
    if isinstance(decoded, payloads.Visit):
        return decoded.start_epoch
    if isinstance(decoded, payloads.Captured):
        return decoded.epoch
    return None


def _tag_of(decoded: object) -> bytes | None:
    if isinstance(decoded, payloads.Visit | payloads.Captured):
        return decoded.tag
    return None


def _fields_of(decoded: object) -> dict[str, object]:
    """The decoded payload's own fields, JSON-safe.

    ``dataclasses.asdict`` wants a ``DataclassInstance``-shaped argument;
    ``decoded`` here is only known as ``object`` (it comes from a
    msg_type-keyed dispatch table), so ``vars()`` does the same job without
    fighting the type checker. Every payload dataclass in this project
    stores its fields as plain instance attributes, so this holds for all of
    them.
    """
    raw = vars(decoded)
    return {key: (value.hex() if isinstance(value, bytes) else value) for key, value in raw.items()}
