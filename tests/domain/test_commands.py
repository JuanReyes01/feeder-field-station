"""What answers a pending downlink command (S1, S2, S4, DD15).

Every test here checks `Command.satisfied_by`/`due_for_resend` against a bare
`ReportedState` fact — never against `domain.traps.Registry` — so this module
never needs to import `traps.py` (traps.py is the one that imports `commands.py`,
to hold a `Command` on a `Trap`; the reverse import would be a cycle).
"""

from datetime import UTC, datetime, timedelta

from feeder_station.domain.commands import Command, CommandKind, ReportedState

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def test_arm_is_satisfied_by_reported_state_armed() -> None:
    """S1/S4: an ARM command is answered once the trap itself reports armed."""
    command = Command(kind=CommandKind.ARM)

    assert command.satisfied_by(ReportedState(armed=False)) is False
    assert command.satisfied_by(ReportedState(armed=True)) is True


def test_disarm_is_satisfied_by_reported_state_not_armed() -> None:
    """DD15, the gap this module exists to close: the pre-DD15 model in
    `traps.py` cleared `arm_pending` only when the reported state was
    `ARMED` — a rule with no way to answer a pending *disarm* at all, since
    a disarmed trap never reports `ARMED`. A `DISARM` command must be
    satisfied by the opposite fact: the trap reporting it is *not* armed.
    """
    command = Command(kind=CommandKind.DISARM)

    assert command.satisfied_by(ReportedState(armed=True)) is False
    assert command.satisfied_by(ReportedState(armed=False)) is True


def test_set_targets_is_satisfied_only_when_reported_version_matches_desired() -> None:
    """S2: delivery is read off the trap's reported `targets_version`, not
    asserted from having sent the frame."""
    command = Command(kind=CommandKind.SET_TARGETS, targets_version=5)

    assert command.satisfied_by(ReportedState(armed=True, targets_version=4)) is False
    assert command.satisfied_by(ReportedState(armed=True, targets_version=5)) is True


def test_due_for_resend_follows_the_capped_backoff_schedule() -> None:
    """S2's resend requirement ("the resend interval elapses" -> sent again)
    follows the same capped-backoff shape as the serial gateway's own
    reconnect loop (design.md section 4): 1, 2, 4... seconds, not a fixed
    interval and not immediate on every tick.
    """
    command = Command(kind=CommandKind.SET_TARGETS, targets_version=5)
    assert command.due_for_resend(NOW) is True  # never sent -> due immediately

    command = command.sent(NOW)
    assert command.attempts == 1
    assert command.due_for_resend(NOW) is False  # just sent -> not due yet
    assert command.due_for_resend(NOW + timedelta(seconds=0.9)) is False
    assert command.due_for_resend(NOW + timedelta(seconds=1.0)) is True  # 1st backoff step

    command = command.sent(NOW + timedelta(seconds=1.0))
    assert command.attempts == 2
    assert command.due_for_resend(NOW + timedelta(seconds=1.0)) is False
    assert command.due_for_resend(NOW + timedelta(seconds=2.9)) is False
    assert command.due_for_resend(NOW + timedelta(seconds=3.0)) is True  # 2nd backoff step
