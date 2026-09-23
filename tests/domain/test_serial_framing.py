"""COBS framing, checked against published reference vectors and corruption.

The block codec (`cobs_encode`/`cobs_decode`) is checked against Wikipedia's
published "Consistent Overhead Byte Stuffing" reference vectors rather than
byte vectors shared with the firmware: no COBS component exists yet on the
firmware side to share vectors with (exploration.md, section 2 — the gap
this slice exists to close). The larger, boundary-crossing sizes are
round-tripped instead of hand-transcribed, since a hand-copied 500-byte
vector is itself a transcription risk this test would rather not carry.

`FrameStream` is checked against exactly what makes COBS worth having:
resynchronising past a corrupted block so the next well-formed one still
decodes, reassembling a frame delivered across arbitrarily small reads, and
never buffering without bound when no delimiter ever arrives.
"""

from __future__ import annotations

import pytest

from feeder_station.domain import frames, serial_framing
from feeder_station.domain.frames import Frame
from feeder_station.domain.serial_framing import CobsError, FrameStream

# https://en.wikipedia.org/wiki/Consistent_Overhead_Byte_Stuffing#Encoding_examples
# Hand-verified against this module's own encode/decode trace, not copied blind.
REFERENCE_VECTORS = [
    (bytes([0x00]), bytes([0x01, 0x01])),
    (bytes([0x00, 0x00]), bytes([0x01, 0x01, 0x01])),
    (bytes([0x00, 0x11, 0x00]), bytes([0x01, 0x02, 0x11, 0x01])),
    (bytes([0x11, 0x22, 0x00, 0x33]), bytes([0x03, 0x11, 0x22, 0x02, 0x33])),
    (bytes([0x11, 0x22, 0x33, 0x44]), bytes([0x05, 0x11, 0x22, 0x33, 0x44])),
    (bytes([0x11, 0x00, 0x00, 0x00]), bytes([0x02, 0x11, 0x01, 0x01, 0x01])),
]


def _wire(*raw_frames: bytes) -> bytes:
    """Concatenate frames as they would actually arrive on the wire."""
    return b"".join(
        serial_framing.cobs_encode(raw) + bytes([serial_framing.DELIMITER]) for raw in raw_frames
    )


@pytest.mark.parametrize("raw, encoded", REFERENCE_VECTORS)
def test_cobs_encode_matches_the_published_reference_vectors(raw: bytes, encoded: bytes) -> None:
    assert serial_framing.cobs_encode(raw) == encoded


@pytest.mark.parametrize("raw, encoded", REFERENCE_VECTORS)
def test_cobs_decode_matches_the_published_reference_vectors(raw: bytes, encoded: bytes) -> None:
    assert serial_framing.cobs_decode(encoded) == raw


@pytest.mark.parametrize("size", [0, 1, 2, 253, 254, 255, 256, 508, 509, 510])
def test_cobs_round_trips_arbitrary_sizes_crossing_the_254_byte_block_boundary(
    size: int,
) -> None:
    # Cycles through 0..250 so every size includes delimiter-colliding zero
    # bytes, not just a run of non-zero data — the case COBS exists for.
    data = bytes(i % 251 for i in range(size))

    encoded = serial_framing.cobs_encode(data)

    assert serial_framing.DELIMITER not in encoded
    assert serial_framing.cobs_decode(encoded) == data


def test_cobs_decode_rejects_a_code_byte_that_overruns_the_block() -> None:
    # code=0xFA claims 249 more bytes; only 3 remain.
    with pytest.raises(CobsError):
        serial_framing.cobs_decode(bytes([0xFA, 0x01, 0x02, 0x03]))


def test_cobs_decode_rejects_a_zero_length_code() -> None:
    with pytest.raises(CobsError):
        serial_framing.cobs_decode(bytes([0x00, 0x01]))


MAX_PAYLOAD_PATTERNS = {
    "all_zero": bytes(200),
    "no_zero": bytes((i % 255) + 1 for i in range(200)),
    "delimiter_colliding": bytes(0x00 if i % 4 == 0 else i for i in range(200)),
}


@pytest.mark.parametrize(
    "payload", MAX_PAYLOAD_PATTERNS.values(), ids=list(MAX_PAYLOAD_PATTERNS.keys())
)
def test_round_trips_a_maximum_size_frame(payload: bytes) -> None:
    original = Frame(msg_type=frames.PING, node_id=0x1234, msg_id=0xBEEF, payload=payload)
    raw = frames.encode(original)
    assert len(raw) == serial_framing.MAX_FRAME_BYTES

    wire_block = serial_framing.cobs_encode(raw)
    assert serial_framing.DELIMITER not in wire_block

    recovered_raw = serial_framing.cobs_decode(wire_block)
    assert recovered_raw == raw
    assert frames.decode(recovered_raw) == original


def test_frame_stream_round_trips_a_single_frame_fed_whole() -> None:
    raw = frames.encode(Frame(msg_type=frames.PING, payload=b"hi"))
    stream = FrameStream()

    assert stream.feed(_wire(raw)) == [raw]
    assert stream.decode_errors == 0


def test_frame_stream_reassembles_a_frame_split_across_several_feed_calls() -> None:
    raw_a = frames.encode(Frame(msg_type=frames.PING, payload=b"first"))
    raw_b = frames.encode(Frame(msg_type=frames.FIRED, payload=b"second"))
    wire = _wire(raw_a, raw_b)
    stream = FrameStream()

    # One byte per feed() call: the worst case for a stream that never
    # arrives in whole frames.
    decoded: list[bytes] = []
    for i in range(len(wire)):
        decoded += stream.feed(wire[i : i + 1])

    assert decoded == [raw_a, raw_b]
    assert stream.decode_errors == 0


def test_resyncs_after_a_corrupted_frame() -> None:
    corrupted = bytes([0xFA, 0x01, 0x02, 0x03])  # guaranteed CobsError, see above
    good = frames.encode(Frame(msg_type=frames.PING, payload=b"still fine"))
    wire = corrupted + bytes([serial_framing.DELIMITER]) + _wire(good)
    stream = FrameStream()

    decoded = stream.feed(wire)

    assert decoded == [good]
    assert stream.decode_errors == 1


def test_a_frame_longer_than_the_maximum_is_rejected_without_unbounded_buffering() -> None:
    stream = FrameStream(max_block_bytes=32)
    # Never zero, never delimits: the runaway-noise case a real link can produce.
    runaway = bytes((i % 255) + 1 for i in range(200))

    decoded_from_runaway = stream.feed(runaway)

    assert decoded_from_runaway == []
    assert stream.decode_errors == 1
    assert stream.buffered_bytes == 0

    # The garbage never contained a delimiter, so the stream is still
    # discarding; the next 0x00 it sees — whenever it arrives — is consumed
    # purely as the resync point, exactly like a truncated block would be.
    resync = stream.feed(bytes([serial_framing.DELIMITER]))
    assert resync == []

    good = frames.encode(Frame(msg_type=frames.PING, payload=b"recovered"))
    decoded_after = stream.feed(_wire(good))

    assert decoded_after == [good]
