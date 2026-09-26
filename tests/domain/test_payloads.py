"""Uplink payload codecs, checked against literal byte vectors.

``messages.md`` pins the field order for every payload type but not every
field's width or encoding. Where a width is not pinned, ``payloads.py`` picks
one and says so in a comment; the vectors here match those choices exactly,
the same way ``test_frames.py`` pins the frame header.
"""

import pytest

from feeder_station.domain import payloads
from feeder_station.domain.payloads import PayloadError
from feeder_station.domain.traps import TrapState

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


def test_encodes_an_ack_payload() -> None:
    result = payloads.encode_ack(acked_msg_id=0xBEEF, status=0)

    assert result == bytes([0xEF, 0xBE, 0x00])


def test_encoded_ack_round_trips_through_decode_ack() -> None:
    result = payloads.encode_ack(acked_msg_id=0x1234, status=2)

    assert payloads.decode_ack(result) == payloads.Ack(acked_msg_id=0x1234, status=2)


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


# ---- READS (0x06): ported from feeder-node-firmware's own encoder test
# vectors ---------------------------------------------------------------
#
# READS is the one uplink message where the usual order is reversed:
# feeder-node-firmware/components/station_proto's own encoder
# (station_encode_reads()) shipped and ran on hardware before this decoder
# existed (messages.md's own note on READS explains why). Its test suite
# (test/test_station_proto/test_station_proto.c) hand-derived these bytes
# straight from messages.md's field order and said, in its own comment,
# that this suite should adopt them as its vectors once decode_reads()
# exists -- exactly what the three constants below do. Deriving these
# bytes a second time, independently, from messages.md's prose instead of
# the firmware's own already-hardware-verified encoder is the drift this
# change exists to avoid: two independent readings of the same document is
# exactly how the C and Python sides end up silently disagreeing while
# both still look correct.

READS_SINGLE = bytes([0x01, 0x01, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0xC0, 0xFF, 0x69, 0x67])

READS_MULTI = bytes(
    [
        0x02,
        0x01,
        0x01,
        0x02,
        0x03,
        0x04,
        0x05,
        0x06,
        0x07,
        0xC0,
        0xFF,
        0x69,
        0x67,
        0xAA,
        0xBB,
        0xCC,
        0xDD,
        0xEE,
        0xFF,
        0x00,
        0x18,
        0x02,
        0x6A,
        0x67,
    ]
)

# clock_trusted = false, epoch = 0 -- messages.md's own ambiguous case
# ("epoch = 0 ... is also a real midnight in 1970"), proven here the same
# way the firmware's own test proves it: the trust byte is read
# independently of whatever the epoch bytes say.
READS_UNTRUSTED = bytes(
    [0x01, 0x00, 0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06, 0x00, 0x00, 0x00, 0x00]
)


def test_decode_reads_reads_a_single_trusted_read() -> None:
    result = payloads.decode_reads(READS_SINGLE)

    assert result.count == 1
    assert result.clock_trusted is True
    assert len(result.reads) == 1
    assert result.reads[0].tag == bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07])
    assert result.reads[0].epoch == 1_735_000_000


def test_decode_reads_keeps_each_reads_own_tag_and_epoch() -> None:
    """Kill: a loop that reuses the first read's tag/epoch for every entry
    (a copy-paste index bug, the same class of bug the firmware's own
    equivalent test names) leaves the second entry wrong here even though
    the single-read test above would still pass.
    """
    result = payloads.decode_reads(READS_MULTI)

    assert result.count == 2
    assert result.clock_trusted is True
    assert result.reads[0].tag == bytes([0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07])
    assert result.reads[0].epoch == 1_735_000_000
    assert result.reads[1].tag == bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF, 0x00])
    assert result.reads[1].epoch == 1_735_000_600


def test_decode_reads_reports_an_untrusted_clock_without_touching_the_epoch() -> None:
    """Kill: a decode_reads() that infers clock_trusted from epoch == 0 (or
    from anything but byte 1 itself) happens to read this vector the same
    way either way -- messages.md's own rule, "Trust is never inferred
    from the epoch's value, on either side", applies to this side exactly
    as much as it does to the encoder's.
    """
    result = payloads.decode_reads(READS_UNTRUSTED)

    assert result.clock_trusted is False
    assert result.reads[0].tag == bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])
    assert result.reads[0].epoch == 0


def test_decode_reads_rejects_a_payload_declaring_zero_reads() -> None:
    """Kill: dropping this guard turns a 2-byte header with count=0 into an
    empty-but-accepted batch instead of refusing it -- the same "not
    well-formed" refusal station_encode_reads() itself already makes on
    the encode side (see its own comment).
    """
    with pytest.raises(PayloadError):
        payloads.decode_reads(bytes([0x00, 0x01]))


def test_decode_reads_rejects_a_length_that_does_not_match_the_declared_count() -> None:
    with pytest.raises(PayloadError):
        payloads.decode_reads(READS_SINGLE[:-1])


def test_decode_reads_rejects_a_payload_longer_than_its_declared_count() -> None:
    """Kill: weakening the length check from "exactly this many bytes" to
    "at least this many bytes" (``_require_min_len`` instead of
    ``_require_len``) still rejects a too-short payload (the test above)
    but silently accepts trailing garbage past the declared batch -- this
    is the direction only an exact-length check catches.
    """
    with pytest.raises(PayloadError):
        payloads.decode_reads(READS_SINGLE + bytes([0x00]))


def test_decode_reads_rejects_a_payload_shorter_than_its_own_header() -> None:
    with pytest.raises(PayloadError):
        payloads.decode_reads(bytes([0x01]))


# --- CRITICAL 2 / WARNING B (sdd-verify, field-station-p4): an out-of-range
# state byte must never reach the store. WARNING B (verify-report-batch1.md):
# a hand-picked list like `[4, 7, 130, 255]` proves the examples, not the
# rule -- a mutation that special-cased exactly those four values stayed
# green. Reject is now every byte `TrapState` doesn't have, derived, not
# listed by hand; accept (below) is every byte it does -- closing the gap
# the report named: nothing anywhere asserted a valid `state=2`/`state=3`
# heartbeat actually decodes.


@pytest.mark.parametrize("bad_state", range(len(TrapState), 256))
def test_decode_heartbeat_rejects_an_out_of_range_state_byte(bad_state: int) -> None:
    raw = bytes(
        [
            bad_state,  # state: out of TrapState's 0-3 range
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

    with pytest.raises(PayloadError):
        payloads.decode_heartbeat(raw)


@pytest.mark.parametrize("bad_state_after", range(len(TrapState), 256))
def test_decode_captured_rejects_an_out_of_range_state_after_byte(bad_state_after: int) -> None:
    raw = bytes(TAG) + bytes(
        [
            0x11,
            0x10,
            0x0F,
            0x0E,  # epoch, little endian: 0x0E0F1011
            bad_state_after,  # state_after: out of TrapState's 0-3 range
        ]
    )

    with pytest.raises(PayloadError):
        payloads.decode_captured(raw)


@pytest.mark.parametrize("state", range(len(TrapState)))
def test_decode_heartbeat_accepts_every_valid_state_value(state: int) -> None:
    raw = bytes(
        [
            state,
            0xA8,
            0x0A,
            0x04,
            0x03,
            0x02,
            0x01,
            0x01,
            0x09,
            0x00,
            0x05,
            0x00,
            0x08,
            0x07,
            0x06,
            0x05,
        ]
    )
    assert payloads.decode_heartbeat(raw).state == state


@pytest.mark.parametrize("state_after", range(len(TrapState)))
def test_decode_captured_accepts_every_valid_state_after_value(state_after: int) -> None:
    raw = bytes(TAG) + bytes([0x11, 0x10, 0x0F, 0x0E, state_after])
    assert payloads.decode_captured(raw).state_after == state_after


def test_encodes_an_arm_payload() -> None:
    result = payloads.encode_arm(expires_epoch=0x01020304)

    assert result == bytes([0x04, 0x03, 0x02, 0x01])


def test_encodes_a_disarm_payload() -> None:
    assert payloads.encode_disarm() == b""


def test_encodes_a_get_state_payload() -> None:
    assert payloads.encode_get_state() == b""


def test_encodes_a_get_backlog_payload() -> None:
    result = payloads.encode_get_backlog(after_msg_id=0xBEEF, max_events=10)

    assert result == bytes([0xEF, 0xBE, 0x0A, 0x00])


def test_encodes_time_sync_with_no_refusal_field() -> None:
    result = payloads.encode_time_sync(epoch=0x01020304)

    assert result == bytes([0x04, 0x03, 0x02, 0x01])
    assert len(result) == 4


def _set_targets_header(frame: bytes) -> tuple[int, int, int, int]:
    targets_version = frame[0] | (frame[1] << 8)
    seq = frame[2] | (frame[3] << 8)
    seq_total = frame[4] | (frame[5] << 8)
    count = frame[6]
    return targets_version, seq, seq_total, count


def test_encodes_a_40_tag_target_list_across_two_frames() -> None:
    tags = [bytes([i, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]) for i in range(40)]

    frames = payloads.encode_set_targets(targets_version=9, tags=tags)

    assert len(frames) == 2
    assert all(len(frame) <= 200 for frame in frames)

    version0, seq0, seq_total0, count0 = _set_targets_header(frames[0])
    version1, seq1, seq_total1, count1 = _set_targets_header(frames[1])

    assert version0 == version1 == 9
    assert seq_total0 == seq_total1 == 2
    assert (seq0, seq1) == (0, 1)
    assert count0 == 27
    assert count1 == 13
    assert frames[0][7:] == b"".join(tags[:27])
    assert frames[1][7:] == b"".join(tags[27:])
