"""What a decoded frame becomes once the station is ready to store it.

``interpret()`` is the boundary between ``domain.frames``/``domain.payloads``
and the row-shape ``adapters/store.py`` persists (design.md, section 2). It
never touches a clock or a database: ``received_epoch`` and ``clock_trust``
are stamped by the store, at the moment of writing, not here.
"""

from __future__ import annotations

import struct

import pytest

from feeder_station.domain import payloads
from feeder_station.domain.events import UninterpretableFrame, interpret, interpret_reads
from feeder_station.domain.frames import Frame

TAG = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])
TAG_2 = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF, 0x00])


def test_interpret_produces_a_station_event_from_a_captured_frame() -> None:
    epoch = 1_727_092_345
    payload = TAG + struct.pack("<I", epoch) + bytes([0x01])  # state_after: armed
    frame = Frame(msg_type=payloads.CAPTURED, node_id=7, msg_id=42, payload=payload)

    event = interpret(frame)

    assert event.source == "node"
    assert event.node_id == 7
    assert event.msg_type == payloads.CAPTURED
    assert event.msg_id == 42
    assert event.tag == TAG
    assert event.node_epoch == epoch
    assert event.fields["state_after"] == 1
    assert event.payload == payload


def test_interpret_a_heartbeat_carries_no_tag_but_carries_its_own_clock_epoch() -> None:
    payload = bytes(
        [0x01, 0xA8, 0x0A, 0x04, 0x03, 0x02, 0x01, 0x01, 0x09, 0x00, 0x05, 0x00, 0x08, 0x07, 0x06, 0x05]
    )
    frame = Frame(msg_type=payloads.HEARTBEAT, node_id=3, msg_id=1, payload=payload)

    event = interpret(frame)

    assert event.tag is None
    assert event.node_epoch == 0x01020304
    assert event.fields["state"] == 1
    assert event.fields["battery_mv"] == 0x0AA8


def test_interpret_a_fault_hex_encodes_its_free_form_detail_bytes() -> None:
    frame = Frame(msg_type=payloads.FAULT, node_id=1, payload=bytes([0x02, 0xDE, 0xAD]))

    event = interpret(frame)

    assert event.node_epoch is None
    assert event.tag is None
    assert event.fields["code"] == 2
    assert event.fields["detail"] == "dead"


def test_interpret_rejects_a_frame_with_no_known_uplink_decoder() -> None:
    frame = Frame(msg_type=payloads.ARM, node_id=1, payload=b"")

    with pytest.raises(UninterpretableFrame):
        interpret(frame)


# --- interpret_reads(): a READS frame is N observations, not one ------------


def _reads_payload(*, clock_trusted: bool, reads: list[tuple[bytes, int]]) -> bytes:
    body = bytes([len(reads), 1 if clock_trusted else 0])
    for tag, epoch in reads:
        body += tag + struct.pack("<I", epoch)
    return body


def test_interpret_reads_produces_one_station_event_per_read() -> None:
    payload = _reads_payload(
        clock_trusted=True, reads=[(TAG, 1_735_000_000), (TAG_2, 1_735_000_600)]
    )
    frame = Frame(msg_type=payloads.READS, node_id=7, msg_id=42, payload=payload)

    events = interpret_reads(frame)

    assert len(events) == 2
    assert [e.tag for e in events] == [TAG, TAG_2]
    assert [e.node_epoch for e in events] == [1_735_000_000, 1_735_000_600]
    for event in events:
        assert event.source == "node"
        assert event.node_id == 7
        assert event.msg_type == payloads.READS
        assert event.payload == payload


def test_interpret_reads_shares_one_msg_id_and_payload_across_the_batch() -> None:
    """The exact fact `adapters/store.py::Store.append_batch` depends on:
    every read in a batch carries the *same* wire `msg_id`, because they
    all came from one frame -- this is the whole reason a batch cannot be
    routed through `Store.append()` once per read (see that module's own
    docstring for what a per-event dedup check does to 16 of these 17).
    """
    payload = _reads_payload(
        clock_trusted=True, reads=[(TAG, 1_735_000_000), (TAG_2, 1_735_000_600)]
    )
    frame = Frame(msg_type=payloads.READS, node_id=7, msg_id=42, payload=payload)

    events = interpret_reads(frame)

    assert {e.msg_id for e in events} == {42}
    assert {e.payload for e in events} == {payload}


def test_interpret_reads_masks_node_epoch_when_the_batch_is_untrusted() -> None:
    """Kill: masking `node_epoch` from whether the epoch is zero (instead
    of from `clock_trusted` itself) would happen to read this the same way
    only when the epoch actually is zero -- here it is deliberately
    nonzero, so an epoch-based mutant reports it as trusted and wrongly
    keeps the number instead of masking it.
    """
    payload = _reads_payload(clock_trusted=False, reads=[(TAG, 1_735_000_000)])
    frame = Frame(msg_type=payloads.READS, node_id=7, msg_id=1, payload=payload)

    events = interpret_reads(frame)

    assert events[0].node_epoch is None
    assert events[0].fields["clock_trusted"] is False
    assert events[0].fields["epoch"] is None


def test_interpret_reads_keeps_a_trusted_zero_epoch() -> None:
    """The other half of the same rule: messages.md's own ambiguous case
    (`epoch == 0`, "also a real midnight in 1970") must not be masked away
    when the batch genuinely is trusted -- trust drives the mask, the
    epoch's value never does, in either direction.
    """
    payload = _reads_payload(clock_trusted=True, reads=[(TAG, 0)])
    frame = Frame(msg_type=payloads.READS, node_id=7, msg_id=1, payload=payload)

    events = interpret_reads(frame)

    assert events[0].node_epoch == 0
    assert events[0].fields["clock_trusted"] is True
    assert events[0].fields["epoch"] == 0


def test_interpret_reads_rejects_a_frame_that_is_not_reads() -> None:
    """Kill: dropping this guard would not be caught by `decode_reads`
    raising on its own -- this payload is deliberately well-formed *as* a
    READS batch, so without the guard `interpret_reads` would silently
    misinterpret a different message type's bytes as reads instead of
    raising. Proves the check is against `frame.msg_type` itself, not
    "whatever decode_reads happens to reject".
    """
    well_formed_reads_payload = _reads_payload(clock_trusted=True, reads=[(TAG, 1_735_000_000)])
    frame = Frame(
        msg_type=payloads.CAPTURED, node_id=1, msg_id=1, payload=well_formed_reads_payload
    )

    with pytest.raises(ValueError, match="READS"):
        interpret_reads(frame)
