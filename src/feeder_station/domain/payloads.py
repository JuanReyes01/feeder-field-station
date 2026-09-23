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

from collections.abc import Sequence
from dataclasses import dataclass

from .frames import PAYLOAD_MAX
from .traps import TrapState

# Uplink message types (docs/protocol/messages.md, "Uplink, node to station").
HELLO = 0x01
HEARTBEAT = 0x02
VISIT = 0x03
CAPTURED = 0x04
FAULT = 0x05
ACK = 0x7F

# Downlink message types (docs/protocol/messages.md, "Downlink, station to node").
ARM = 0x81
DISARM = 0x82
SET_TARGETS = 0x83
GET_STATE = 0x84
TIME_SYNC = 0x85
GET_BACKLOG = 0x86

TAG_BYTES = 7

# SET_TARGETS header: targets_version (2) + seq (2) + seq_total (2) + count (1).
_SET_TARGETS_HEADER_BYTES = 7
# How many tags fit in one 200-byte payload alongside that header — this is
# what pins messages.md's "a 200-byte payload carries 27 tags" (200-7)//7=27.
TAGS_PER_SET_TARGETS_FRAME = (PAYLOAD_MAX - _SET_TARGETS_HEADER_BYTES) // TAG_BYTES


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


# HEARTBEAT.state and CAPTURED.state_after: matches TrapState's declaration
# order in adapters/store.py's `_WIRE_STATE_ORDER`. WARNING B (sdd-verify
# field-station-p4 batch 1): derived from `len(TrapState)`, not a
# hand-written `range(4)` -- a hardcoded literal stayed green through a
# mutation that shrank it to `range(3)`, silently rejecting a valid
# `state=3` (FAULT) heartbeat. A future TrapState member now widens this
# automatically instead of drifting out of sync in silence.
_VALID_STATE_VALUES = range(len(TrapState))


def _require_valid_state(value: int, field_name: str, payload_name: str) -> None:
    if value not in _VALID_STATE_VALUES:
        raise PayloadError(
            f"{payload_name} {field_name} byte {value} has no known TrapState (expected 0-3)"
        )


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
    state = payload[0]
    _require_valid_state(state, "state", "HEARTBEAT")
    return Heartbeat(
        state=state,
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
    state_after = payload[11]
    _require_valid_state(state_after, "state_after", "CAPTURED")
    tag = payload[0:7]
    return Captured(
        tag=tag,
        epoch=(payload[7] | (payload[8] << 8) | (payload[9] << 16) | (payload[10] << 24)),
        state_after=state_after,
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


def encode_arm(*, expires_epoch: int) -> bytes:
    # expires_epoch: 4 bytes LE uint32 — pinned; it is a timestamp, and
    # messages.md's "Timestamps are UTC seconds since the Unix epoch, uint32"
    # convention applies to every epoch field, not only the uplink ones.
    return bytes(
        [
            expires_epoch & 0xFF,
            (expires_epoch >> 8) & 0xFF,
            (expires_epoch >> 16) & 0xFF,
            (expires_epoch >> 24) & 0xFF,
        ]
    )


def encode_disarm() -> bytes:
    # DISARM has no payload (messages.md: "--").
    return b""


def encode_get_state() -> bytes:
    # GET_STATE has no payload; it is answered with HEARTBEAT (messages.md).
    return b""


def encode_time_sync(*, epoch: int) -> bytes:
    # epoch: 4 bytes LE uint32 — pinned (Timestamps convention). No confidence
    # or refusal field: the protocol has none (D7); a station that must not
    # send TIME_SYNC withholds the frame entirely instead of encoding one.
    return bytes(
        [
            epoch & 0xFF,
            (epoch >> 8) & 0xFF,
            (epoch >> 16) & 0xFF,
            (epoch >> 24) & 0xFF,
        ]
    )


def encode_get_backlog(*, after_msg_id: int, max_events: int) -> bytes:
    # after_msg_id: 2 bytes LE — pinned, the same width as the frame header's
    # own msg_id field (same choice already made for ACK's acked_msg_id).
    # max_events: 2 bytes LE — assumption; a batch-size counter, sized like
    # reads_today/duration_s rather than a frame-bounded field, since nothing
    # in the protocol caps how many events a station may ask for in one turn.
    return bytes(
        [
            after_msg_id & 0xFF,
            (after_msg_id >> 8) & 0xFF,
            max_events & 0xFF,
            (max_events >> 8) & 0xFF,
        ]
    )


def encode_ack(*, acked_msg_id: int, status: int = 0) -> bytes:
    # acked_msg_id: 2 bytes LE, status: 1 byte — pinned, the exact mirror of
    # decode_ack's layout. messages.md lists `0x7F ACK` under *both* the
    # uplink and downlink catalogues with the identical payload shape: a
    # node ACKs a downlink command, and the station ACKs an uplink frame
    # that asked for one (ack_required, frame header flag bit0). Only the
    # decode half existed before this slice wired the station's own ACK
    # send (adapters/ingest.py, S9's ingest-and-ACK ordering).
    return bytes(
        [
            acked_msg_id & 0xFF,
            (acked_msg_id >> 8) & 0xFF,
            status & 0xFF,
        ]
    )


def _chunk_tags(tags: Sequence[bytes]) -> list[Sequence[bytes]]:
    """Split a tag list into groups no larger than one frame can carry.

    Always returns at least one chunk (possibly empty) so an empty target
    list still produces a single SET_TARGETS frame declaring `count=0`.
    """
    if not tags:
        return [()]
    return [
        tags[start : start + TAGS_PER_SET_TARGETS_FRAME]
        for start in range(0, len(tags), TAGS_PER_SET_TARGETS_FRAME)
    ]


def encode_set_targets(*, targets_version: int, tags: Sequence[bytes]) -> list[bytes]:
    # targets_version: 2 bytes LE — reused, not a fresh assumption: it is the
    # same value HEARTBEAT/HELLO report back, already fixed at that width.
    # seq, seq_total: 2 bytes LE each — assumption; counters sized like
    # reads_today/boot_count, since nothing bounds how many frames a very
    # long target list could need.
    # count: 1 byte — assumption; bounded by frame capacity (<=27, see
    # TAGS_PER_SET_TARGETS_FRAME above), mirroring frames.py's own
    # frame-bounded `payload_len` field rather than the open-ended counters.
    for tag in tags:
        _require_len(tag, TAG_BYTES, "SET_TARGETS tag")

    chunks = _chunk_tags(tags)
    seq_total = len(chunks)
    frames = []
    for seq, chunk in enumerate(chunks):
        header = bytes(
            [
                targets_version & 0xFF,
                (targets_version >> 8) & 0xFF,
                seq & 0xFF,
                (seq >> 8) & 0xFF,
                seq_total & 0xFF,
                (seq_total >> 8) & 0xFF,
                len(chunk),
            ]
        )
        frames.append(header + b"".join(chunk))
    return frames
