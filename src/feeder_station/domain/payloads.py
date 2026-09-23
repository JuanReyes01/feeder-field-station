"""Uplink and downlink payload bodies of the v1 node<->station protocol.

Defined once in the project's ``docs/protocol/messages.md``; this module holds
the payloads that ride inside a ``domain.frames.Frame`` after its header. The
header already carries ``msg_type`` and ``payload_len``, and ``frames.decode``
has already checked the declared length against what arrived, so a decoder
here only has to check its own type's fixed layout against what it was given.

``messages.md`` pins some field widths explicitly (timestamps are `uint32`,
`tag` is the reader's 7 raw bytes) and leaves others unstated. Where a width
is not pinned, this module picks one and says so in a comment, following
``frames.py``'s convention: little endian, an explicit length, no int wider
than the value plausibly needs. These are implementation assumptions, not the
protocol document's word — a future revision of ``messages.md`` that pins a
width explicitly wins over the comment.
"""

from __future__ import annotations

from dataclasses import dataclass

# Uplink message types (docs/protocol/messages.md, "Uplink, node to station").
HELLO = 0x01
HEARTBEAT = 0x02
VISIT = 0x03
CAPTURED = 0x04
FAULT = 0x05
ACK = 0x7F


class PayloadError(ValueError):
    """A payload that is not well-formed for its declared message type."""


def _require_len(payload: bytes, expected: int, name: str) -> None:
    """Check the length before any field is read.

    A short payload must never yield a partially-constructed value: the
    length is checked first, so a truncated frame fails loudly instead of
    reading past its own end (the same discipline ``frames.decode`` already
    applies to the header).
    """
    if len(payload) != expected:
        raise PayloadError(
            f"{name} payload of {len(payload)} bytes, expected exactly {expected}"
        )


def _require_min_len(payload: bytes, minimum: int, name: str) -> None:
    if len(payload) < minimum:
        raise PayloadError(f"{name} payload of {len(payload)} bytes, expected at least {minimum}")


@dataclass(frozen=True)
class Hello:
    fw_version: tuple[int, int, int]
    app: int
    targets_version: int
    boot_count: int


def decode_hello(payload: bytes) -> Hello:
    # fw_version[3]: pinned by messages.md's bracket notation, same as tag[7].
    # app: 1 byte — assumption; a 2-value enum needs no more, matching flags/hops.
    # targets_version, boot_count: 2 bytes LE each — assumption; sized like the
    # header's node_id/msg_id rather than the header's 1-byte fields, because
    # both can plausibly exceed 255 over a node's lifetime.
    _require_len(payload, 8, "HELLO")
    return Hello(
        fw_version=(payload[0], payload[1], payload[2]),
        app=payload[3],
        targets_version=payload[4] | (payload[5] << 8),
        boot_count=payload[6] | (payload[7] << 8),
    )


@dataclass(frozen=True)
class Heartbeat:
    state: int
    battery_mv: int
    clock_epoch: int
    clock_source: int
    targets_version: int
    reads_today: int
    last_read_epoch: int


def decode_heartbeat(payload: bytes) -> Heartbeat:
    # state, clock_source: 1 byte each — assumption; small enums (0-3, 0-2)
    # matching the header's 1-byte flags/hops fields.
    # battery_mv: 2 bytes LE — assumption; millivolts needs more than a byte.
    # clock_epoch, last_read_epoch: 4 bytes LE uint32 — pinned by messages.md's
    # "Timestamps are UTC seconds since the Unix epoch, uint32".
    # targets_version, reads_today: 2 bytes LE each — assumption, as in HELLO.
    _require_len(payload, 16, "HEARTBEAT")
    return Heartbeat(
        state=payload[0],
        battery_mv=payload[1] | (payload[2] << 8),
        clock_epoch=(payload[3] | (payload[4] << 8) | (payload[5] << 16) | (payload[6] << 24)),
        clock_source=payload[7],
        targets_version=payload[8] | (payload[9] << 8),
        reads_today=payload[10] | (payload[11] << 8),
        last_read_epoch=(
            payload[12] | (payload[13] << 8) | (payload[14] << 16) | (payload[15] << 24)
        ),
    )


@dataclass(frozen=True)
class Visit:
    tag: bytes
    start_epoch: int
    duration_s: int
    read_count: int


def decode_visit(payload: bytes) -> Visit:
    # tag[7]: pinned (the reader's 7 raw bytes).
    # start_epoch: 4 bytes LE uint32 — pinned (Timestamps convention above).
    # duration_s, read_count: 2 bytes LE each — assumption; a visit's duration
    # and read count can plausibly exceed 255.
    _require_len(payload, 15, "VISIT")
    tag = payload[0:7]
    return Visit(
        tag=tag,
        start_epoch=(payload[7] | (payload[8] << 8) | (payload[9] << 16) | (payload[10] << 24)),
        duration_s=payload[11] | (payload[12] << 8),
        read_count=payload[13] | (payload[14] << 8),
    )


@dataclass(frozen=True)
class Captured:
    tag: bytes
    epoch: int
    state_after: int


def decode_captured(payload: bytes) -> Captured:
    # tag[7]: pinned. epoch: 4 bytes LE uint32 — pinned (Timestamps convention).
    # state_after: 1 byte — assumption; same small enum as HEARTBEAT.state.
    _require_len(payload, 12, "CAPTURED")
    tag = payload[0:7]
    return Captured(
        tag=tag,
        epoch=(payload[7] | (payload[8] << 8) | (payload[9] << 16) | (payload[10] << 24)),
        state_after=payload[11],
    )


@dataclass(frozen=True)
class Fault:
    code: int
    detail: bytes


def decode_fault(payload: bytes) -> Fault:
    # code: 1 byte — assumption, a status-code-sized enum.
    # detail: the rest of the payload — assumption; messages.md's "detail[..]"
    # bracket notation reads as a variable-length trailing field, with no
    # separate length prefix needed since the frame header's payload_len
    # already bounds the whole payload.
    _require_min_len(payload, 1, "FAULT")
    return Fault(code=payload[0], detail=bytes(payload[1:]))


@dataclass(frozen=True)
class Ack:
    acked_msg_id: int
    status: int


def decode_ack(payload: bytes) -> Ack:
    # acked_msg_id: 2 bytes LE — pinned, the same width as the frame header's
    # own msg_id field.
    # status: 1 byte — assumption, a status-code-sized enum.
    _require_len(payload, 3, "ACK")
    return Ack(
        acked_msg_id=payload[0] | (payload[1] << 8),
        status=payload[2],
    )
