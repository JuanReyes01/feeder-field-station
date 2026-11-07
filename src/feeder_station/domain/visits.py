"""Readings grouped into visits, for what the operator sees.

The node sends individual readings; a visit is an interpretation of them. The
default gap is the one ``feeder-analysis`` uses for hummingbirds, so the station
and the analysis count the same thing. Nothing is stored: visits are computed
from the readings every time, and can never fall behind them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

DEFAULT_GAP_SECONDS = 3.0


@dataclass(frozen=True)
class Reading:
    node_id: int
    tag: bytes
    epoch: float
    clock_trusted: bool


@dataclass(frozen=True)
class Visit:
    node_id: int
    tag: bytes
    start: float
    end: float
    reads: int
    clock_trusted: bool

    @property
    def duration(self) -> float:
        return self.end - self.start


def group_visits(
    readings: Iterable[Reading], gap_seconds: float = DEFAULT_GAP_SECONDS
) -> list[Visit]:
    """One visit per run of readings of a tag at a trap, newest first.

    A tag at two traps is two visits, and two tags at one trap never merge.
    """
    open_visits: dict[tuple[int, bytes], Visit] = {}
    closed: list[Visit] = []
    for reading in sorted(readings, key=lambda r: r.epoch):
        key = (reading.node_id, reading.tag)
        current = open_visits.get(key)
        if current is not None and reading.epoch - current.end <= gap_seconds:
            open_visits[key] = Visit(
                node_id=current.node_id,
                tag=current.tag,
                start=current.start,
                end=reading.epoch,
                reads=current.reads + 1,
                clock_trusted=current.clock_trusted and reading.clock_trusted,
            )
            continue
        if current is not None:
            closed.append(current)
        open_visits[key] = Visit(
            node_id=reading.node_id,
            tag=reading.tag,
            start=reading.epoch,
            end=reading.epoch,
            reads=1,
            clock_trusted=reading.clock_trusted,
        )
    closed.extend(open_visits.values())
    return sorted(closed, key=lambda v: v.start, reverse=True)
