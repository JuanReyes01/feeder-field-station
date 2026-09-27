"""``domain/targets_reassembly.py`` -- the station's own fold of a `TARGETS`
uplink (docs/protocol/messages.md) back into one list, the mirror of
`domain/payloads.py::encode_set_targets`'s chunking, read backwards. Pure:
no store, no registry, no wire bytes -- every test here drives
``accept_chunk`` directly.
"""

from __future__ import annotations

from feeder_station.domain.targets_reassembly import accept_chunk

TAG_A = bytes([0x0A] * 7)
TAG_B = bytes([0x0B] * 7)
TAG_C = bytes([0x0C] * 7)


def test_a_single_chunk_generation_completes_immediately() -> None:
    state, completed = accept_chunk(
        None, targets_version=1, seq=0, seq_total=1, tags=[TAG_A, TAG_B]
    )

    assert state is None
    assert completed == (TAG_A, TAG_B)


def test_an_empty_target_list_completes_with_zero_tags() -> None:
    state, completed = accept_chunk(None, targets_version=1, seq=0, seq_total=1, tags=[])

    assert state is None
    assert completed == ()


def test_a_split_generation_does_not_complete_until_every_part_arrives() -> None:
    state, completed = accept_chunk(None, targets_version=9, seq=0, seq_total=2, tags=[TAG_A])

    assert completed is None
    assert state is not None

    state, completed = accept_chunk(
        state, targets_version=9, seq=1, seq_total=2, tags=[TAG_B]
    )

    assert state is None
    assert completed == (TAG_A, TAG_B)  # in seq order


def test_parts_arriving_out_of_order_still_combine_in_seq_order() -> None:
    state, completed = accept_chunk(None, targets_version=9, seq=1, seq_total=2, tags=[TAG_B])
    assert completed is None

    state, completed = accept_chunk(
        state, targets_version=9, seq=0, seq_total=2, tags=[TAG_A]
    )

    assert completed == (TAG_A, TAG_B)  # seq 0 before seq 1, regardless of arrival order


def test_a_resent_duplicate_part_does_not_complete_early() -> None:
    """The wire's own at-least-once retry: the same part arriving twice
    must not be counted as two of three parts.
    """
    state, completed = accept_chunk(None, targets_version=9, seq=0, seq_total=3, tags=[TAG_A])
    assert completed is None

    # A retransmission of the exact same part 0 -- overwrites its own slot,
    # never advances completion.
    state, completed = accept_chunk(
        state, targets_version=9, seq=0, seq_total=3, tags=[TAG_A]
    )
    assert completed is None
    assert state is not None
    assert len(state.chunks) == 1


def test_a_chunk_naming_a_different_targets_version_discards_the_in_progress_reassembly() -> None:
    """The explicit choice `docs/protocol/messages.md`'s own `GET_TARGETS`
    section records: a part of a new generation is proof the node has
    already moved past the old one, so the old, partial reassembly can
    never complete -- keeping it around risks splicing a stale generation's
    tags in beside a newer one's.
    """
    state, completed = accept_chunk(None, targets_version=5, seq=0, seq_total=2, tags=[TAG_A])
    assert completed is None

    # Version 6's own first (and only) part -- completes on its own.
    state, completed = accept_chunk(
        state, targets_version=6, seq=0, seq_total=1, tags=[TAG_B]
    )
    assert completed == (TAG_B,)
    assert state is None

    # Version 5's stray second part, arriving late: version 5's own part 0
    # was discarded the moment version 6's chunk arrived, so this starts a
    # *fresh*, still-incomplete version-5 reassembly rather than completing
    # the original one.
    state, completed = accept_chunk(
        state, targets_version=5, seq=1, seq_total=2, tags=[TAG_C]
    )
    assert completed is None
    assert state is not None
    assert state.targets_version == 5
    assert set(state.chunks) == {1}  # only the just-arrived part -- part 0 is gone


def test_a_chunk_naming_the_same_version_but_a_different_seq_total_also_discards() -> None:
    """Defensive: a station never re-chunks one edit two different ways,
    but a `seq_total` mismatch for what would otherwise read as the same
    generation is treated exactly like a version mismatch -- never merged.
    """
    state, completed = accept_chunk(None, targets_version=9, seq=0, seq_total=3, tags=[TAG_A])
    assert completed is None

    state, completed = accept_chunk(
        state, targets_version=9, seq=0, seq_total=1, tags=[TAG_B]
    )

    assert completed == (TAG_B,)  # the re-chunked single-part reading, standalone
