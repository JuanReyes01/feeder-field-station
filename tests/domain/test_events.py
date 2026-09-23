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
from feeder_station.domain.events import UninterpretableFrame, interpret
from feeder_station.domain.frames import Frame

TAG = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])


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
