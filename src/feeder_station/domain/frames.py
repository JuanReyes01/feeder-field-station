"""The wire frame shared by the station and the nodes.

Defined once in the project's ``docs/protocol/messages.md``; this is the Python
half. The C half is ``components/link_frame`` in feeder-node-firmware, and the
tests on both sides use the same byte vectors so the two cannot drift apart.

    0    proto_ver
    1    msg_type
    2-3  node_id      (little endian)
    4-5  msg_id       (little endian)
    6    flags
    7    hops         reserved, 0 in v1
    8    payload_len
    9..  payload
"""

from __future__ import annotations

from dataclasses import dataclass, field

PROTO_VER = 1
HEADER_BYTES = 9
PAYLOAD_MAX = 200

# Trigger link, reader <-> release. The station sees these only in passing.
FIRE = 0xA0
FIRED = 0xA1
FIRE_FAILED = 0xA2
PING = 0xA3
RELEASE_STATE = 0xA4

FLAG_ACK_REQUIRED = 0x01
FLAG_RETRANSMIT = 0x02

BROADCAST_NODE_ID = 0xFFFF


class FrameError(ValueError):
    """A buffer that is not a well-formed frame of this protocol version."""


@dataclass(frozen=True)
class Frame:
    msg_type: int
    node_id: int = 0
    msg_id: int = 0
    flags: int = 0
    hops: int = 0
    payload: bytes = field(default=b"")


def encode(frame: Frame) -> bytes:
    if len(frame.payload) > PAYLOAD_MAX:
        raise FrameError(f"payload of {len(frame.payload)} bytes exceeds {PAYLOAD_MAX}")

    header = bytes(
        [
            PROTO_VER,
            frame.msg_type,
            frame.node_id & 0xFF,
            (frame.node_id >> 8) & 0xFF,
            frame.msg_id & 0xFF,
            (frame.msg_id >> 8) & 0xFF,
            frame.flags,
            frame.hops,
            len(frame.payload),
        ]
    )
    return header + frame.payload


def decode(raw: bytes) -> Frame:
    if len(raw) < HEADER_BYTES:
        raise FrameError(f"{len(raw)} bytes is shorter than a {HEADER_BYTES}-byte header")

    if raw[0] != PROTO_VER:
        raise FrameError(f"protocol version {raw[0]}, expected {PROTO_VER}")

    payload_len = raw[8]
    if payload_len > PAYLOAD_MAX:
        raise FrameError(f"declared payload of {payload_len} bytes exceeds {PAYLOAD_MAX}")

    # The declared length has to match what actually arrived. Trusting the byte
    # over the buffer is how a short frame reads past its own end.
    if len(raw) != HEADER_BYTES + payload_len:
        raise FrameError(
            f"declared payload of {payload_len} bytes, but {len(raw) - HEADER_BYTES} arrived"
        )

    return Frame(
        msg_type=raw[1],
        node_id=raw[2] | (raw[3] << 8),
        msg_id=raw[4] | (raw[5] << 8),
        flags=raw[6],
        hops=raw[7],
        payload=bytes(raw[HEADER_BYTES:]),
    )
