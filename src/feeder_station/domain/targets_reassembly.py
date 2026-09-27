"""Station-side reassembly of a `TARGETS` uplink (docs/protocol/messages.md)
-- the mirror of `domain/payloads.py::encode_set_targets`'s own chunking,
read backwards. There, the station splits a list into `seq_total` frames
sharing one `targets_version`; here, the station folds those same frames
back into one list, applied only once every part has arrived -- the same
"applied only once every part has arrived" rule `SET_TARGETS` already
follows in the other direction (`docs/protocol/messages.md`, "A 200-byte
payload carries 27 tags...").

Pure and stateless, the same discipline `domain/backlog.py` already applies
to its own round-robin schedule: no I/O, no clock, no wire bytes. The caller
(`domain/traps.py::Registry.reported_targets_chunk`) owns the one
`ReassemblyState | None` this folds across calls -- one per node, for
whichever generation is currently in progress.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ReassemblyState:
    """One `targets_version`'s parts collected so far.

    `chunks` is keyed by `seq`, not appended to a list: a duplicate
    delivery of the same part (the wire's own at-least-once retry, or a
    resend the node made before hearing anything back) overwrites its own
    slot rather than growing past `seq_total` on a re-send.
    """

    targets_version: int
    seq_total: int
    chunks: Mapping[int, tuple[bytes, ...]]


def accept_chunk(
    state: ReassemblyState | None,
    *,
    targets_version: int,
    seq: int,
    seq_total: int,
    tags: Sequence[bytes],
) -> tuple[ReassemblyState | None, tuple[bytes, ...] | None]:
    """Fold one `TARGETS` chunk into `state`.

    Returns ``(next_state, completed_tags)``. ``completed_tags`` is
    ``None`` until every one of `seq_total` parts has arrived for the
    *same* generation, at which point it is the whole list, in `seq`
    order, and `next_state` is ``None`` -- nothing left in progress.

    **A chunk naming a different `targets_version` (or, defensively, a
    different `seq_total` for what would otherwise read as the same
    version -- a station never re-chunks one edit two different ways)
    than the reassembly already in progress discards that reassembly
    outright and starts a fresh one from this chunk alone, rather than
    merging across two generations.** This is the explicit choice
    `docs/protocol/messages.md`'s own `GET_TARGETS` section records, and
    it is not a policy this module could safely relax later without
    revisiting that document: a part of a new generation is proof the
    node has already moved past the old one (`targets_version` only ever
    increases, one bump per edit -- `Registry.command_set_targets`'s own
    per-edit version). The old, partial reassembly can therefore never
    complete honestly; keeping it around any longer only risks eventually
    splicing a stale generation's tags in beside a newer one's, which is
    worse than simply losing an incomplete answer the node itself has
    already superseded. The station will ask again anyway --
    `Registry._note_targets_mismatch` re-arms a fetch for as long as the
    reported version disagrees with the trap's own reported
    `targets_version`.
    """
    if state is None or state.targets_version != targets_version or state.seq_total != seq_total:
        state = ReassemblyState(targets_version=targets_version, seq_total=seq_total, chunks={})

    chunks = dict(state.chunks)
    chunks[seq] = tuple(tags)
    state = replace(state, chunks=chunks)

    if len(chunks) >= seq_total and all(i in chunks for i in range(seq_total)):
        combined = tuple(tag for i in range(seq_total) for tag in chunks[i])
        return None, combined
    return state, None
