"""7.1/7.3 — the pure round-robin rotation schedule (domain/backlog.py).

No I/O, no clock, no wire bytes, and no cursor arithmetic: whether a node
"answered" is a fact the caller supplies, the same discipline
``domain/traps.py`` already applies to ``now``.
"""

from __future__ import annotations

import pytest

from feeder_station.domain import backlog


def test_rotation_visits_each_node_once_per_round() -> None:
    node_ids = (11, 12, 13, 14, 15)
    schedule = backlog.start(node_ids)

    visited = []
    for _ in range(len(node_ids)):
        visited.append(backlog.current_target(schedule))
        schedule = backlog.record_response(schedule, heard=True, retry_budget=3)

    assert visited == list(node_ids)
    # a full lap wraps back to the first node
    assert backlog.current_target(schedule) == node_ids[0]


def test_one_silent_node_does_not_block_the_rotation() -> None:
    """Radio-gateway spec.md, "one silent node does not block the rotation":
    ten nodes, one of which never responds, and the rotation still reaches
    all nine others rather than looping forever on the silent one.
    """
    node_ids = tuple(range(10))
    silent_node = 4
    retry_budget = 3
    schedule = backlog.start(node_ids)

    visited = []
    # Bounded on purpose: (nodes - 1) single-attempt turns plus retry_budget
    # attempts spent on the one silent node. If the rotation ever blocked on
    # node 4 instead of moving on, this loop -- which never sleeps and never
    # waits on real I/O -- would simply produce a `visited` list that never
    # advances past node 4, and the exact-sequence assertion below fails.
    for _ in range(len(node_ids) - 1 + retry_budget):
        node_id = backlog.current_target(schedule)
        visited.append(node_id)
        schedule = backlog.record_response(
            schedule, heard=node_id != silent_node, retry_budget=retry_budget
        )

    assert visited == [0, 1, 2, 3, 4, 4, 4, 5, 6, 7, 8, 9]
    assert set(visited) == set(node_ids)
    assert backlog.current_target(schedule) == node_ids[0]


def test_retry_budget_must_be_at_least_one() -> None:
    schedule = backlog.start((1,))

    with pytest.raises(ValueError, match="retry_budget"):
        backlog.record_response(schedule, heard=False, retry_budget=0)


def test_empty_roster_is_rejected() -> None:
    with pytest.raises(ValueError, match="node"):
        backlog.start(())
