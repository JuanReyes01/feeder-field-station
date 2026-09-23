"""The wire frame, checked against the firmware's own test vectors.

These are the same bytes as ``test_link_frame.c`` in feeder-node-firmware. Two
languages implement one contract, and this file is what stops them drifting
apart: if the C side changes a field's offset, these tests fail here.
"""

import pytest

from feeder_station.domain import frames
from feeder_station.domain.frames import Frame, FrameError

TAG = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])


def test_the_header_lays_out_as_documented() -> None:
    raw = frames.encode(Frame(msg_type=frames.PING, node_id=0x0201, msg_id=0x0403))

    assert raw == bytes(
        [
            0x01,  # proto_ver
            0xA3,  # msg_type: PING
            0x01,
            0x02,  # node_id, little endian
            0x03,
            0x04,  # msg_id, little endian
            0x00,  # flags
            0x00,  # hops: reserved, zero in v1
            0x00,  # payload_len
        ]
    )


def test_a_frame_survives_the_round_trip() -> None:
    sent = Frame(
        msg_type=frames.FIRE,
        node_id=0x1234,
        msg_id=0xBEEF,
        flags=frames.FLAG_ACK_REQUIRED,
        payload=TAG,
    )

    got = frames.decode(frames.encode(sent))

    assert got == sent


def test_a_fire_frame_matches_the_firmware_byte_for_byte() -> None:
    raw = frames.encode(
        Frame(
            msg_type=frames.FIRE,
            node_id=0x1234,
            msg_id=0xBEEF,
            flags=frames.FLAG_ACK_REQUIRED,
            payload=TAG,
        )
    )

    assert raw.hex(" ") == "01 a0 34 12 ef be 01 00 07 03 84 19 9c 87 ec 06"


def test_a_truncated_frame_is_refused() -> None:
    raw = frames.encode(Frame(msg_type=frames.FIRED, payload=b"abcd"))

    for cut in (len(raw) - 1, frames.HEADER_BYTES - 1, 0):
        with pytest.raises(FrameError):
            frames.decode(raw[:cut])


def test_another_protocol_version_is_refused() -> None:
    raw = bytearray(frames.encode(Frame(msg_type=frames.PING)))
    raw[0] = frames.PROTO_VER + 1

    with pytest.raises(FrameError):
        frames.decode(bytes(raw))


def test_a_lying_payload_length_is_refused() -> None:
    raw = bytearray(frames.encode(Frame(msg_type=frames.PING)))
    raw[8] = 50  # claims fifty bytes that never arrived

    with pytest.raises(FrameError):
        frames.decode(bytes(raw))


def test_a_payload_that_does_not_fit_is_refused() -> None:
    with pytest.raises(FrameError):
        frames.encode(Frame(msg_type=frames.FIRE, payload=b"x" * (frames.PAYLOAD_MAX + 1)))
