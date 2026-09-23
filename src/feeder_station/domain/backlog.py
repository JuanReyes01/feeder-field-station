"""Round-robin polling order across the node roster (S5, S9).

Pure scheduling only: which node's turn is next, and how many consecutive
silent attempts a node gets before the rotation moves past it. No I/O, no
clock, no wire bytes — and deliberately no cursor arithmetic at all.

``GET_BACKLOG``'s ``after_msg_id`` cursor is ``adapters/store.py::Store
.cursor()``'s business, not this module's: design.md's DD7 makes
``node_cursor.last_msg_id`` the *most recently accepted* ``msg_id``,
written in the same transaction as every accepted event, ordered by the
station's own ``seq`` — never ``MAX(msg_id)``. ``MAX`` reads correctly right
up until a node's 16-bit wire counter wraps ``0xFFFF -> 0x0000`` and is then
permanently wrong: the station would ask for everything after an id the
node has already passed and never catch up again. This module never reads,
stores, or compares a ``msg_id``, so there is no cursor logic here that
could reintroduce that bug — the adapter (``adapters/scheduler.py``) only
ever forwards whatever ``Store.cursor()`` returns.

Whether a node "answered" is likewise a fact the caller supplies
(``heard``), the same discipline ``domain/traps.py`` already applies to
``now`` and ``silence_after``: a pure module is handed the world's answer,
it never asks for it itself.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class RotationSchedule:
    """One fixed roster, whose turn is next, and how many silent attempts
    the node currently at bat has had in a row.
    """

    node_ids: tuple[int, ...]
    position: int = 0
    attempts: int = 0

    def __post_init__(self) -> None:
        if not self.node_ids:
            raise ValueError("a rotation needs at least one node")


def start(node_ids: Sequence[int]) -> RotationSchedule:
    """Begin a rotation at the first node in ``node_ids``, in that order."""
    return RotationSchedule(node_ids=tuple(node_ids))


def current_target(schedule: RotationSchedule) -> int:
    """The node whose turn it is right now."""
    return schedule.node_ids[schedule.position]


def record_response(
    schedule: RotationSchedule, *, heard: bool, retry_budget: int
) -> RotationSchedule:
    """Advance the schedule after one attempt against ``current_target(schedule)``.

    ``heard=True`` moves on to the next node immediately, with a clean
    retry count. ``heard=False`` keeps the same node's turn — up to
    ``retry_budget`` consecutive silent attempts — so one unlucky beat does
    not skip a node that would have answered the next try. Once the budget
    is spent, the rotation moves on regardless: this is what keeps one
    silent node (radio-gateway spec.md, "one silent node does not block the
    rotation") from stalling the other nine. That node's next turn comes
    back around on the following lap, not blocked on here.
    """
    if retry_budget < 1:
        raise ValueError("retry_budget must be at least 1")

    if heard or schedule.attempts + 1 >= retry_budget:
        next_position = (schedule.position + 1) % len(schedule.node_ids)
        return RotationSchedule(schedule.node_ids, next_position, 0)
    return RotationSchedule(schedule.node_ids, schedule.position, schedule.attempts + 1)
