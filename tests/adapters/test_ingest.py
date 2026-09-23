"""``adapters/ingest.py`` — the only place that knows the S9 ordering.

decode -> write + fsync (event-store) -> ACK on the radio -> registry update
-> publish SSE, in that literal order. Only the first two steps are
S9-critical (design.md, section 3): an ACK must never leave the station for
an event that is not already durably committed. Every fake here is a plain
Protocol double (DD16's own reasoning, one layer up): no SQLite, no pyserial.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from feeder_station.adapters.ingest import ingest_frame
from feeder_station.domain import payloads
from feeder_station.domain.frames import FLAG_ACK_REQUIRED, Frame, encode
from feeder_station.domain.traps import Registry, TrapState

TAG = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])
RECEIVED_EPOCH = 1_727_092_345.0


class _Appended:
    seq = 1
    node_seq = 1
    duplicate_of = None


class _StubStore:
    def append(self, event, *, received_epoch, clock_trust):
        return _Appended()


class _RaisingStore:
    def append(self, event, *, received_epoch, clock_trust):
        raise RuntimeError("disk full")


class _SpyAckPort:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write_frame(self, raw_frame: bytes) -> bool:
        self.written.append(raw_frame)
        return True


class _OrderedStore:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def append(self, event, *, received_epoch, clock_trust):
        self._calls.append("store.append")
        return _Appended()


class _OrderedAckPort:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls
        self.written: list[bytes] = []

    def write_frame(self, raw_frame: bytes) -> bool:
        self._calls.append("ack.write")
        self.written.append(raw_frame)
        return True


class _OrderedRegistry(Registry):
    def __init__(self, calls: list[str]) -> None:
        super().__init__(silence_after=timedelta(minutes=15))
        self._calls = calls

    def captured(self, node_id: int, tag: bytes, state: TrapState, at: datetime):
        self._calls.append("registry.captured")
        return super().captured(node_id, tag, state, at)


def _captured_frame(node_id: int = 7, msg_id: int = 1) -> bytes:
    payload = TAG + bytes([0x11, 0x10, 0x0F, 0x0E, 0x02])  # epoch + state_after=fired
    return encode(
        Frame(
            msg_type=payloads.CAPTURED,
            node_id=node_id,
            msg_id=msg_id,
            flags=FLAG_ACK_REQUIRED,
            payload=payload,
        )
    )


def _heartbeat_frame(node_id: int = 3, msg_id: int = 9) -> bytes:
    payload = bytes(
        [0x01, 0xA8, 0x0A, 0x04, 0x03, 0x02, 0x01, 0x01, 0x09, 0x00, 0x05, 0x00, 0x08, 0x07, 0x06, 0x05]
    )
    return encode(
        Frame(
            msg_type=payloads.HEARTBEAT,
            node_id=node_id,
            msg_id=msg_id,
            flags=FLAG_ACK_REQUIRED,
            payload=payload,
        )
    )


def _fault_frame(node_id: int = 5, msg_id: int = 2) -> bytes:
    payload = bytes([0x02]) + b"sd-full"
    return encode(
        Frame(
            msg_type=payloads.FAULT,
            node_id=node_id,
            msg_id=msg_id,
            flags=FLAG_ACK_REQUIRED,
            payload=payload,
        )
    )


# --- 6.3/6.4: no ACK may leave the station for an event that is not committed


def test_ack_withheld_on_write_failure() -> None:
    ack_port = _SpyAckPort()
    registry = Registry(silence_after=timedelta(minutes=15))

    with pytest.raises(RuntimeError):
        ingest_frame(
            _heartbeat_frame(),
            store=_RaisingStore(),
            ack_port=ack_port,
            registry=registry,
            publish=lambda event: None,
            received_epoch=RECEIVED_EPOCH,
            clock_trust="rtc",
        )

    assert ack_port.written == []


# --- 6.5/6.6: the store write is observably complete before anything else --


def test_registry_updates_after_the_write_not_before() -> None:
    """This is the test that would fail if the S9 ordering were reversed:
    if the ACK were sent, or the registry updated, before `store.append()`
    returns, `calls` would not read `["store.append", "ack.write",
    "registry.captured"]` any more.
    """
    calls: list[str] = []
    store = _OrderedStore(calls)
    ack_port = _OrderedAckPort(calls)
    registry = _OrderedRegistry(calls)

    ingest_frame(
        _captured_frame(),
        store=store,
        ack_port=ack_port,
        registry=registry,
        publish=lambda event: None,
        received_epoch=RECEIVED_EPOCH,
        clock_trust="rtc",
    )

    assert calls == ["store.append", "ack.write", "registry.captured"]
    assert ack_port.written != []


# --- 6.7/6.8: a FAULT uplink updates the reported state to fault -----------


def test_fault_uplink_updates_reported_state_to_fault() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))

    ingest_frame(
        _fault_frame(node_id=5),
        store=_StubStore(),
        ack_port=_SpyAckPort(),
        registry=registry,
        publish=lambda event: None,
        received_epoch=RECEIVED_EPOCH,
        clock_trust="rtc",
    )

    assert registry.trap(5).state is TrapState.FAULT


# --- ACK is only sent for frames that asked for one -------------------------


def test_ack_is_withheld_when_the_frame_did_not_request_one() -> None:
    ack_port = _SpyAckPort()
    registry = Registry(silence_after=timedelta(minutes=15))
    raw = encode(Frame(msg_type=payloads.HEARTBEAT, node_id=3, msg_id=1, payload=bytes(16)))

    ingest_frame(
        raw,
        store=_StubStore(),
        ack_port=ack_port,
        registry=registry,
        publish=lambda event: None,
        received_epoch=RECEIVED_EPOCH,
        clock_trust="rtc",
    )

    assert ack_port.written == []


# --- an undecodable frame is dropped: no ACK, no crash ----------------------


def test_an_undecodable_frame_is_dropped_with_no_ack() -> None:
    ack_port = _SpyAckPort()
    registry = Registry(silence_after=timedelta(minutes=15))
    published: list[dict[str, object]] = []

    ingest_frame(
        b"\x01\x02\x03",  # too short to even be a header
        store=_StubStore(),
        ack_port=ack_port,
        registry=registry,
        publish=published.append,
        received_epoch=RECEIVED_EPOCH,
        clock_trust="rtc",
    )

    assert ack_port.written == []
    assert published == []


def test_an_undecodable_frame_is_counted_via_record_payload_error() -> None:
    """WARNING C (sdd-verify field-station-p4, verify-report-batch1.md):
    before this, a payload-level decode failure was dropped with no
    counter and no log anywhere.
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    payload_errors: list[None] = []

    ingest_frame(
        b"\x01\x02\x03",  # too short to even be a header
        store=_StubStore(),
        ack_port=_SpyAckPort(),
        registry=registry,
        publish=lambda event: None,
        received_epoch=RECEIVED_EPOCH,
        clock_trust="rtc",
        record_payload_error=lambda: payload_errors.append(None),
    )

    assert payload_errors == [None]


def test_a_dropped_frame_is_logged_at_warning_with_the_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WARNING K(a) (verify-report-batch1-round2.md): mutation testing
    proved this log is the only thing that justifies dropping an
    undecodable frame silently -- downgrading it to
    `logger.debug("ingest: dropped a frame")`, dropping the exception text
    that names *why* the frame was undecodable, left all 729 tests green.
    """
    registry = Registry(silence_after=timedelta(minutes=15))

    with caplog.at_level(logging.WARNING, logger="feeder_station.adapters.ingest"):
        ingest_frame(
            b"\x01\x02\x03",  # too short to even be a header -- raises FrameError
            store=_StubStore(),
            ack_port=_SpyAckPort(),
            registry=registry,
            publish=lambda event: None,
            received_epoch=RECEIVED_EPOCH,
            clock_trust="rtc",
        )

    drop_records = [r for r in caplog.records if "dropped an undecodable frame" in r.getMessage()]
    assert len(drop_records) == 1
    record = drop_records[0]
    assert record.levelno == logging.WARNING  # not downgraded to DEBUG
    # the exception itself, not dropped -- FrameError's own message for 3 bytes
    assert "shorter than" in record.getMessage()
