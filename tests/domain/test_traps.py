"""The trap registry: what the station believes about each trap, and why.

Two rules from the requirements drive every test here:

  S4  — state comes from what the trap reports, never from what we commanded.
  S3  — a capture is a state the trap stays in, not a notification that flies by.
"""

from datetime import UTC, datetime, timedelta

from feeder_station.domain import payloads
from feeder_station.domain.commands import Command, CommandKind
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
    assert registry.trap(7).pending_command == Command(kind=CommandKind.ARM)

    registry.heartbeat(node_id=7, state=TrapState.ARMED, at=NOW + timedelta(seconds=30))
    assert registry.trap(7).state is TrapState.ARMED
    assert registry.trap(7).pending_command is None


def test_disarm_pending_clears_when_the_trap_reports_not_armed() -> None:
    """DD15: the pre-DD15 model cleared `arm_pending` only when a heartbeat
    reported `ARMED`, which has no way to answer a pending *disarm* -- a
    disarmed trap never reports `ARMED`, so a disarm command would stay
    pending forever under that rule. `command_disarm` + `Command
    .satisfied_by` must clear it the moment the trap reports anything other
    than armed.
    """
    registry = Registry(silence_after=timedelta(minutes=5))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, at=NOW)

    registry.command_disarm(7, at=NOW)
    assert registry.trap(7).state is TrapState.ARMED  # S4: still what it last reported
    assert registry.trap(7).pending_command == Command(kind=CommandKind.DISARM)

    # A heartbeat that still reports ARMED must NOT satisfy a pending disarm.
    registry.heartbeat(node_id=7, state=TrapState.ARMED, at=NOW + timedelta(seconds=30))
    assert registry.trap(7).pending_command == Command(kind=CommandKind.DISARM)

    registry.heartbeat(node_id=7, state=TrapState.IDLE, at=NOW + timedelta(seconds=60))
    assert registry.trap(7).state is TrapState.IDLE
    assert registry.trap(7).pending_command is None


def test_edit_is_pending_until_confirmed() -> None:
    """S2: editing a trap's target list must show as pending, not delivered,
    until a HEARTBEAT reports the new version back -- the view must
    distinguish what the trap has confirmed from what the station desires.
    """
    registry = Registry(silence_after=timedelta(minutes=5))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, targets_version=4, at=NOW)

    registry.command_set_targets(7, targets_version=5, at=NOW)

    view = registry.view(now=NOW)[0]
    assert view["targets_version"] == 4  # still what the trap last confirmed
    assert view["desired_targets_version"] == 5
    assert view["pending_command"] == "set_targets"


def test_resend_continues_until_version_confirmed() -> None:
    """S2: `SET_TARGETS` sent for version N, and a HEARTBEAT still reporting
    version N-1, must leave the command pending -- that pending state is
    what drives the resend. Only a HEARTBEAT reporting version N clears it.
    """
    registry = Registry(silence_after=timedelta(minutes=5))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, targets_version=4, at=NOW)
    registry.command_set_targets(7, targets_version=5, at=NOW)

    registry.heartbeat(
        node_id=7, state=TrapState.ARMED, targets_version=4, at=NOW + timedelta(minutes=1)
    )
    assert registry.trap(7).pending_command == Command(
        kind=CommandKind.SET_TARGETS, targets_version=5
    )

    registry.heartbeat(
        node_id=7, state=TrapState.ARMED, targets_version=5, at=NOW + timedelta(minutes=2)
    )
    assert registry.trap(7).pending_command is None
    assert registry.trap(7).targets_version == 5


def test_split_list_is_not_marked_delivered_until_version_matches() -> None:
    """S2: a target list split across two `SET_TARGETS` frames sharing one
    `targets_version` is marked delivered from the version match alone --
    the station never tracks which of the two frames arrived separately.
    """
    tags = [bytes([i, 0, 0, 0, 0, 0, 0]) for i in range(30)]  # > one frame's 27-tag limit
    frames = payloads.encode_set_targets(targets_version=9, tags=tags)
    assert len(frames) == 2  # confirms this scenario actually exercises the split

    registry = Registry(silence_after=timedelta(minutes=5))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, targets_version=8, at=NOW)
    registry.command_set_targets(7, targets_version=9, at=NOW)
    assert registry.trap(7).pending_command is not None  # both frames still "in flight"

    registry.heartbeat(
        node_id=7, state=TrapState.ARMED, targets_version=9, at=NOW + timedelta(seconds=5)
    )
    assert registry.trap(7).pending_command is None
    assert registry.trap(7).targets_version == 9


def test_a_capture_stays_until_someone_acknowledges_it() -> None:
    """S3: whoever opens the page six hours later still sees it."""
    registry = Registry(silence_after=timedelta(minutes=5))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, at=NOW)

    registry.captured(node_id=7, tag=TAG, state=TrapState.FIRED, at=NOW + timedelta(minutes=1))

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


def test_a_fault_uplink_sets_the_trap_to_fault_state() -> None:
    """radio-gateway spec.md: "On a FAULT uplink, the system MUST update the
    affected trap's reported state to fault." The node itself reported this
    — same standing as any other heartbeat-shaped fact (S4), not a guess
    this station made about a silent node (that is `is_reachable()`'s job).
    """
    registry = Registry(silence_after=timedelta(minutes=5))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, at=NOW)

    registry.fault(7, at=NOW + timedelta(minutes=1))

    trap = registry.trap(7)
    assert trap.state is TrapState.FAULT
    assert trap.last_seen == NOW + timedelta(minutes=1)


def test_traps_are_listed_in_a_stable_order() -> None:
    registry = Registry(silence_after=timedelta(minutes=5))
    for node in (9, 3, 7):
        registry.heartbeat(node_id=node, state=TrapState.IDLE, at=NOW)

    assert [t.node_id for t in registry.traps()] == [3, 7, 9]
