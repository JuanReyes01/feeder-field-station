"""The trap registry: what the station believes about each trap, and why.

Two rules from the requirements drive every test here:

  S4  — state comes from what the trap reports, never from what we commanded.
  S3  — a capture is a state the trap stays in, not a notification that flies by.
"""

from datetime import UTC, datetime, timedelta

from feeder_station.domain.traps import Registry, TrapState

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
TAG = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])


def test_a_trap_is_unknown_until_it_speaks() -> None:
    registry = Registry(silence_after=timedelta(minutes=5))
    assert registry.traps() == []


def test_a_heartbeat_creates_and_updates_a_trap() -> None:
    registry = Registry(silence_after=timedelta(minutes=5))

    registry.heartbeat(node_id=7, state=TrapState.ARMED, battery_mv=3900,
                       targets_version=2, at=NOW)

    trap = registry.trap(7)
    assert trap.state is TrapState.ARMED
    assert trap.battery_mv == 3900
    assert trap.targets_version == 2
    assert trap.last_seen == NOW


def test_arming_is_pending_until_the_trap_itself_reports_it() -> None:
    """S4: the UI must never show a command as if it were a fact."""
    registry = Registry(silence_after=timedelta(minutes=5))
    registry.heartbeat(node_id=7, state=TrapState.IDLE, at=NOW)

    registry.command_arm(7, at=NOW)
    assert registry.trap(7).state is TrapState.IDLE
    assert registry.trap(7).arm_pending is True

    registry.heartbeat(node_id=7, state=TrapState.ARMED, at=NOW + timedelta(seconds=30))
    assert registry.trap(7).state is TrapState.ARMED
    assert registry.trap(7).arm_pending is False


def test_a_capture_stays_until_someone_acknowledges_it() -> None:
    """S3: whoever opens the page six hours later still sees it."""
    registry = Registry(silence_after=timedelta(minutes=5))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, at=NOW)

    registry.captured(node_id=7, tag=TAG, at=NOW + timedelta(minutes=1))

    much_later = NOW + timedelta(hours=6)
    capture = registry.trap(7).capture
    assert capture is not None
    assert capture.tag == TAG
    assert capture.acknowledged is False
    assert registry.unacknowledged_captures(now=much_later) != []

    registry.acknowledge(7, at=much_later)
    assert registry.trap(7).capture is None
    assert registry.unacknowledged_captures(now=much_later) == []


def test_silence_is_reported_as_a_fault() -> None:
    """S11: a trap that stops answering must not keep looking healthy."""
    registry = Registry(silence_after=timedelta(minutes=5))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, at=NOW)

    assert registry.trap(7).is_reachable(now=NOW + timedelta(minutes=4)) is True
    assert registry.trap(7).is_reachable(now=NOW + timedelta(minutes=6)) is False


def test_a_silent_trap_is_not_reported_as_armed() -> None:
    """An armed trap nobody can reach cannot catch anything."""
    registry = Registry(silence_after=timedelta(minutes=5))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, at=NOW)

    view = registry.view(now=NOW + timedelta(minutes=10))[0]
    assert view["reachable"] is False
    assert view["state"] == "armed"
    assert view["trustworthy"] is False


def test_traps_are_listed_in_a_stable_order() -> None:
    registry = Registry(silence_after=timedelta(minutes=5))
    for node in (9, 3, 7):
        registry.heartbeat(node_id=node, state=TrapState.IDLE, at=NOW)

    assert [t.node_id for t in registry.traps()] == [3, 7, 9]
