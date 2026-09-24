"""7.6 — wiring `poll_node`'s cursor forwarding to real `GET_BACKLOG`
frames: the store's cursor and the codec.

The wrap test below is the regression proof for design.md's DD7: this
adapter must forward whatever `Store.cursor()` says, never recompute a
`MAX(msg_id)` of its own. `Store.cursor()` already gets this right (slice 4,
`test_store.py::test_msg_id_wraps_without_dropping_or_duplicating`) — what
this module could still get wrong is silently discarding that correctness
by reaching for `MAX` again at the point it builds the outbound frame.

**CRITICAL 5 (sdd-verify field-station-p4, batch 4): this file used to
also cover `run_rotation_round`.** That primitive had zero production
callers and is deleted, not rewired; its poll-order coverage moved to
`tests/adapters/test_workers.py` against `run_scheduler_loop`, the real
caller. What remains here was never about the dead primitive.
"""

from __future__ import annotations

from pathlib import Path

from feeder_station.adapters.scheduler import poll_node
from feeder_station.adapters.store import Store
from feeder_station.domain import frames, payloads
from feeder_station.domain.events import StationEvent


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
