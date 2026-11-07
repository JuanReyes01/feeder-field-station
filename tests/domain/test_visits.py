from feeder_station.domain.visits import Reading, group_visits

TAG_A = b"\x03\x84\x00\x00\x00\x00\x01"
TAG_B = b"\x03\x84\x00\x00\x00\x00\x02"


def read(epoch: float, tag: bytes = TAG_A, node: int = 1, trusted: bool = True) -> Reading:
    return Reading(node_id=node, tag=tag, epoch=epoch, clock_trusted=trusted)


def test_reads_within_the_gap_are_one_visit() -> None:
    [visit] = group_visits([read(10), read(12), read(15)])
    assert (visit.start, visit.end, visit.reads, visit.duration) == (10, 15, 3, 5)


def test_a_silence_longer_than_the_gap_starts_a_new_visit_newest_first() -> None:
    visits = group_visits([read(10), read(14)])
    assert [v.start for v in visits] == [14, 10]


def test_the_same_tag_at_two_traps_never_merges() -> None:
    assert len(group_visits([read(10, node=1), read(11, node=2)])) == 2


def test_two_tags_at_one_trap_never_merge_or_interrupt_each_other() -> None:
    visits = group_visits([read(10), read(11, TAG_B), read(12)])
    assert sorted(v.reads for v in visits) == [1, 2]


def test_one_untrusted_read_makes_the_visits_time_doubtful() -> None:
    [visit] = group_visits([read(10), read(11, trusted=False)])
    assert visit.clock_trusted is False
