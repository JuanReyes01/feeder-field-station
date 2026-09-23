"""Adapter wiring for backlog rotation (S5, S9): the only place that turns
``domain/backlog.py``'s pure round-robin schedule into ``GET_BACKLOG`` frames
on the wire.

Two questions this module deliberately does not answer itself, both taken
as a caller-supplied fact instead:

- **What cursor do we ask a node to catch up from?** ``adapters/store.py::
  Store.cursor()`` already answers this correctly (design.md DD7):
  ``node_cursor.last_msg_id`` is written in the same transaction as every
  accepted event, ordered by the station's own ``seq`` — recency, never
  ``MAX(msg_id)``. Recomputing a maximum here instead (say, by scanning
  ``events_since()``) would reintroduce exactly the bug DD7 exists to
  avoid: once a node's 16-bit ``msg_id`` wraps ``0xFFFF -> 0x0000``, a
  ``MAX`` answer is wrong forever after. This module only ever forwards
  whatever ``Store.cursor()`` returns.
- **Did the node answer?** Whatever counts as "heard" is a fact about link
  or registry state this module does not own; ``run_rotation_round``'s
  caller supplies it via ``responded``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from feeder_station.adapters.store import Cursor
from feeder_station.domain import backlog, frames, payloads
from feeder_station.domain.frames import Frame

# messages.md does not pin how many backlog events a station may ask for in
# one GET_BACKLOG; this is an implementation choice, not a protocol value,
# following the same "assumption, documented" convention domain/payloads.py
# already uses for unpinned field widths.
DEFAULT_MAX_BACKLOG_EVENTS = 32


class CursorSource(Protocol):
    """The one call this module needs from ``adapters/store.py::Store``."""

    def cursor(self, node_id: int) -> Cursor | None: ...


class BacklogGateway(Protocol):
    """The one call this module needs from ``SerialGateway``."""

    def write_frame(self, raw_frame: bytes) -> bool: ...


def poll_node(
    node_id: int,
    *,
    store: CursorSource,
    gateway: BacklogGateway,
    max_events: int = DEFAULT_MAX_BACKLOG_EVENTS,
) -> bool:
    """Ask ``node_id`` for everything after its stored cursor. One frame,
    one write. ``after_msg_id`` is ``0`` — "ask for everything" — for a
    node the store has never accepted an event from yet.
    """
    cursor = store.cursor(node_id)
    after_msg_id = cursor.last_msg_id if cursor is not None else 0
    frame = Frame(
        msg_type=payloads.GET_BACKLOG,
        node_id=node_id,
        payload=payloads.encode_get_backlog(after_msg_id=after_msg_id, max_events=max_events),
    )
    return gateway.write_frame(frames.encode(frame))


def run_rotation_round(
    node_ids: Sequence[int],
    *,
    store: CursorSource,
    gateway: BacklogGateway,
    responded: Callable[[int], bool],
    retry_budget: int,
    max_events: int = DEFAULT_MAX_BACKLOG_EVENTS,
) -> list[int]:
    """Poll the whole roster once, in round-robin order.

    Returns the node ids polled, in the order they were polled — a silent
    node (``responded`` returns ``False`` for it) appears up to
    ``retry_budget`` times in a row before the rotation moves on to the
    next node; it gets another turn on the caller's next call, not blocked
    on here (radio-gateway spec.md, "one silent node does not block the
    rotation").
    """
    schedule = backlog.start(node_ids)
    polled: list[int] = []
    laps_completed = 0
    total_nodes = len(schedule.node_ids)

    while laps_completed < total_nodes:
        node_id = backlog.current_target(schedule)
        polled.append(node_id)
        poll_node(node_id, store=store, gateway=gateway, max_events=max_events)

        prior_position = schedule.position
        schedule = backlog.record_response(
            schedule, heard=responded(node_id), retry_budget=retry_budget
        )
        if schedule.position != prior_position:
            laps_completed += 1

    return polled
