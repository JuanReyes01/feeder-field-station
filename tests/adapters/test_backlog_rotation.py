"""7.5/7.6 — wiring `domain/backlog.py`'s rotation to real `GET_BACKLOG`
frames: the store's cursor, the codec, and the gateway's write.

The wrap test below is the regression proof for design.md's DD7: this
adapter must forward whatever `Store.cursor()` says, never recompute a
`MAX(msg_id)` of its own. `Store.cursor()` already gets this right (slice 4,
`test_store.py::test_msg_id_wraps_without_dropping_or_duplicating`) — what
this module could still get wrong is silently discarding that correctness
by reaching for `MAX` again at the point it builds the outbound frame.
"""

from __future__ import annotations

import os
import pty
import select
import threading
import time
from pathlib import Path

import serial

from feeder_station.adapters.scheduler import poll_node, run_rotation_round
from feeder_station.adapters.serial_gateway import DeviceConfig, SerialGateway
from feeder_station.adapters.store import Store
from feeder_station.domain import frames, payloads
from feeder_station.domain.events import StationEvent
from feeder_station.domain.serial_framing import cobs_decode


def _node_event(*, node_id: int, msg_id: int) -> StationEvent:
    return StationEvent(
        source="node",
        node_id=node_id,
        msg_type=payloads.HEARTBEAT,
        msg_id=msg_id,
        node_epoch=None,
        tag=None,
        fields={},
        payload=b"",
    )


class _FakeGateway:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write_frame(self, raw_frame: bytes) -> bool:
        self.written.append(raw_frame)
        return True


def _get_backlog_after_msg_id(raw_frame: bytes) -> int:
    frame = frames.decode(raw_frame)
    assert frame.msg_type == payloads.GET_BACKLOG
    return frame.payload[0] | (frame.payload[1] << 8)


# --- 7.6: cursor wiring, and the DD7 wrap regression --------------------------


def test_poll_node_asks_after_the_stored_cursor(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")
    store.append(_node_event(node_id=1, msg_id=41), received_epoch=1.0, clock_trust="unset")
    gateway = _FakeGateway()

    poll_node(1, store=store, gateway=gateway, max_events=32)

    assert len(gateway.written) == 1
    assert _get_backlog_after_msg_id(gateway.written[0]) == 41


def test_poll_node_asks_for_everything_when_no_cursor_exists_yet(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")
    gateway = _FakeGateway()

    poll_node(9, store=store, gateway=gateway, max_events=32)

    assert _get_backlog_after_msg_id(gateway.written[0]) == 0


def test_get_backlog_cursor_survives_a_msg_id_wrap(tmp_path: Path) -> None:
    """The trap this slice exists to avoid: `MAX(msg_id)` would read `0xFFFF`
    here (the largest value ever seen), which is exactly wrong — the node's
    counter has moved on to `0x0001` and a station still asking after
    `0xFFFF` would wait forever for a value the node will never send again.
    """
    store = Store(tmp_path / "station.db")
    for msg_id in (0xFFFE, 0xFFFF, 0x0000, 0x0001):
        store.append(
            _node_event(node_id=3, msg_id=msg_id),
            received_epoch=float(msg_id),
            clock_trust="unset",
        )
    gateway = _FakeGateway()

    poll_node(3, store=store, gateway=gateway, max_events=32)

    after_msg_id = _get_backlog_after_msg_id(gateway.written[0])
    assert after_msg_id == 0x0001
    assert after_msg_id != 0xFFFF  # what a naive MAX(msg_id) would have produced


# --- 7.6: full rotation round, fake gateway -----------------------------------


def test_run_rotation_round_visits_every_node_and_retries_the_silent_one(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")
    gateway = _FakeGateway()
    node_ids = tuple(range(10))
    silent_node = 4

    polled = run_rotation_round(
        node_ids,
        store=store,
        gateway=gateway,
        responded=lambda node_id: node_id != silent_node,
        retry_budget=3,
        max_events=32,
    )

    assert polled == [0, 1, 2, 3, 4, 4, 4, 5, 6, 7, 8, 9]
    assert len(gateway.written) == len(polled)
    assert {frames.decode(f).node_id for f in gateway.written} == set(node_ids)


def test_run_rotation_round_terminates_with_a_single_silent_node(tmp_path: Path) -> None:
    """Regression, slice 12: with exactly one node in the roster,
    `domain/backlog.py`'s `position` never changes value even after a full
    "lap" -- `0 % 1` is always `0`. Detecting a completed lap by comparing
    positions before and after (as this function used to) never fires for
    a one-node roster, so the loop never terminated -- exactly the
    scenario a freshly-booted station with one known trap hits for real.
    Run on its own thread with a bounded join so a reintroduced version of
    this bug fails the assertion instead of hanging the whole suite.
    """
    store = Store(tmp_path / "station.db")
    gateway = _FakeGateway()
    result: list[int] = []

    def _run() -> None:
        result.extend(
            run_rotation_round(
                (5,),
                store=store,
                gateway=gateway,
                responded=lambda node_id: False,
                retry_budget=3,
                max_events=32,
            )
        )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=2.0)

    assert not thread.is_alive(), "run_rotation_round did not terminate for a single-node roster"
    assert result == [5, 5, 5]
    assert len(gateway.written) == 3


# --- 7.5: the S5 scale scenario, real bytes over a real pty -------------------


def _open_real_port(device: str) -> serial.Serial:
    return serial.Serial(device, baudrate=115_200, timeout=0.05)


def test_ten_simulated_nodes_over_pty_stays_responsive(tmp_path: Path) -> None:
    """Ten nodes, one silent, polled through a real pyserial port. The
    rotation must complete quickly: nothing here waits for a reply — a
    write is a write — so one silent node costs at most `retry_budget`
    extra writes, never a stall.
    """
    store = Store(tmp_path / "station.db")
    node_ids = tuple(range(1, 11))
    silent_node = 6
    for node_id in node_ids:
        if node_id != silent_node:
            store.append(
                _node_event(node_id=node_id, msg_id=node_id * 10),
                received_epoch=1.0,
                clock_trust="unset",
            )

    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    os.close(slave_fd)
    gateway = SerialGateway(DeviceConfig(explicit_path=slave_name), open_port=_open_real_port)
    try:
        assert gateway.ensure_connected() is True

        started = time.monotonic()
        polled = run_rotation_round(
            node_ids,
            store=store,
            gateway=gateway,
            responded=lambda node_id: node_id != silent_node,
            retry_budget=3,
            max_events=32,
        )
        elapsed = time.monotonic() - started

        assert elapsed < 1.0
        assert set(polled) == set(node_ids)
        assert polled.count(silent_node) == 3

        # Real bytes really crossed the real port: drain them and confirm
        # every write decodes to a GET_BACKLOG addressed to the node it
        # claims, carrying that node's own stored cursor. The last write's
        # bytes are not guaranteed to have reached the master side of the
        # pty by the instant `write_frame` returns, so this polls for them
        # rather than trusting a single `os.read`.
        wire = b""
        deadline = time.monotonic() + 1.0
        while wire.count(bytes([0x00])) < len(polled) and time.monotonic() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 0.05)
            if ready:
                wire += os.read(master_fd, 4096)
        decoded = []
        rest = wire
        while rest:
            block, _, rest = rest.partition(bytes([0x00]))
            if block:
                decoded.append(frames.decode(cobs_decode(block)))
        assert [f.node_id for f in decoded] == list(polled)
        for f in decoded:
            expected_cursor = 0 if f.node_id == silent_node else f.node_id * 10
            assert (f.payload[0] | (f.payload[1] << 8)) == expected_cursor
    finally:
        gateway.close()
        try:
            os.close(master_fd)
        except OSError:
            pass
