"""Uplink payload codecs, checked against literal byte vectors.

``messages.md`` pins the field order for every payload type but not every
field's width or encoding. Where a width is not pinned, ``payloads.py`` picks
one and says so in a comment; the vectors here match those choices exactly,
the same way ``test_frames.py`` pins the frame header.
"""

import pytest

from feeder_station.domain import payloads
from feeder_station.domain.payloads import PayloadError

TAG = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])


def test_decodes_a_heartbeat_payload() -> None:
    raw = bytes(
        [
            0x01,  # state: armed
            0xA8,
            0x0A,  # battery_mv, little endian: 2728
            0x04,
            0x03,
            0x02,
            0x01,  # clock_epoch, little endian: 0x01020304
            0x01,  # clock_source: rtc
            0x09,
            0x00,  # targets_version, little endian: 9
            0x05,
            0x00,  # reads_today, little endian: 5
            0x08,
            0x07,
            0x06,
            0x05,  # last_read_epoch, little endian: 0x05060708
        ]
    )

    result = payloads.decode_heartbeat(raw)

    assert result.state == 1
    assert result.battery_mv == 2728
    assert result.clock_epoch == 0x01020304
    assert result.clock_source == 1
    assert result.targets_version == 9
    assert result.reads_today == 5
    assert result.last_read_epoch == 0x05060708


def test_decodes_a_hello_payload() -> None:
    raw = bytes(
        [
            0x01,
            0x02,
            0x03,  # fw_version[3]
            0x01,  # app: trap
            0x07,
            0x00,  # targets_version, little endian: 7
            0x00,
            0x01,  # boot_count, little endian: 256
        ]
    )

    result = payloads.decode_hello(raw)

    assert result.fw_version == (1, 2, 3)
    assert result.app == 1
    assert result.targets_version == 7
    assert result.boot_count == 256


def test_decodes_a_visit_payload() -> None:
    raw = bytes(TAG) + bytes(
        [
            0x0D,
            0x0C,
            0x0B,
            0x0A,  # start_epoch, little endian: 0x0A0B0C0D
            0x21,
            0x00,  # duration_s, little endian: 33
            0x04,
            0x00,  # read_count, little endian: 4
        ]
    )

    result = payloads.decode_visit(raw)

    assert result.tag == TAG
    assert result.start_epoch == 0x0A0B0C0D
    assert result.duration_s == 33
    assert result.read_count == 4


def test_decodes_a_captured_payload() -> None:
    raw = bytes(TAG) + bytes(
        [
            0x11,
            0x10,
            0x0F,
            0x0E,  # epoch, little endian: 0x0E0F1011
            0x02,  # state_after: fired
        ]
    )

    result = payloads.decode_captured(raw)

    assert result.tag == TAG
    assert result.epoch == 0x0E0F1011
    assert result.state_after == 2


def test_rejects_a_truncated_captured_payload() -> None:
    raw = bytes(TAG) + bytes(
        [
            0x11,
            0x10,
            0x0F,
            0x0E,  # epoch, little endian: 0x0E0F1011
            0x02,  # state_after: fired
        ]
    )

    with pytest.raises(PayloadError):
        payloads.decode_captured(raw[:-1])


def test_decodes_a_fault_payload() -> None:
    raw = bytes([0x02]) + b"sd-full"

    result = payloads.decode_fault(raw)

    assert result.code == 2
    assert result.detail == b"sd-full"


def test_decodes_a_fault_payload_with_no_detail() -> None:
    result = payloads.decode_fault(bytes([0x01]))

    assert result.code == 1
    assert result.detail == b""


def test_decodes_an_ack_payload() -> None:
    raw = bytes(
        [
            0xEF,
            0xBE,  # acked_msg_id, little endian: 0xBEEF
            0x00,  # status: ok
        ]
    )

    result = payloads.decode_ack(raw)

    assert result.acked_msg_id == 0xBEEF
    assert result.status == 0


@pytest.mark.parametrize(
    ("decoder", "valid_length"),
    [
        (payloads.decode_hello, 8),
        (payloads.decode_heartbeat, 16),
        (payloads.decode_visit, 15),
        (payloads.decode_captured, 12),
        (payloads.decode_ack, 3),
    ],
)
def test_a_fixed_length_payload_one_byte_short_is_rejected(decoder, valid_length):
    with pytest.raises(PayloadError):
        decoder(bytes(valid_length - 1))


def test_a_fault_payload_with_zero_bytes_is_rejected() -> None:
    with pytest.raises(PayloadError):
        payloads.decode_fault(b"")
