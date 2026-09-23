"""``adapters/command_sender.py`` — the last of slice 12's four orphaned
drivers: ``domain/commands.py``'s ``due_for_resend()``/``sent()`` (slice 8)
had no caller until this module. ARM and DISARM are sent for real; a
pending SET_TARGETS is deliberately left untouched — see the module's own
docstring for why encoding one here would be guessing, not sending.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from feeder_station.adapters.command_sender import send_due_commands
from feeder_station.domain import payloads
from feeder_station.domain.frames import decode
from feeder_station.domain.traps import Registry, TrapState

NOW = datetime(2026, 1, 1, tzinfo=UTC)


class _SpyGateway:
    def __init__(self, *, accept: bool = True) -> None:
        self.written: list[bytes] = []
        self._accept = accept

    def write_frame(self, raw_frame: bytes) -> bool:
        if self._accept:
            self.written.append(raw_frame)
        return self._accept


# --- a fresh ARM command is due immediately, and is sent for real ----------


def test_a_never_sent_arm_command_is_sent_and_marked() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.command_arm(7, at=NOW)
    gateway = _SpyGateway()

    sent_to = send_due_commands(registry, gateway=gateway, now=NOW, arm_duration_s=86_400)

    assert sent_to == [7]
    assert len(gateway.written) == 1
    frame = decode(gateway.written[0])
    assert frame.msg_type == payloads.ARM
    assert frame.node_id == 7
    expires_epoch = (
        frame.payload[0]
        | (frame.payload[1] << 8)
        | (frame.payload[2] << 16)
        | (frame.payload[3] << 24)
    )
    assert expires_epoch == int(NOW.timestamp()) + 86_400
    # sent() must have advanced attempts/last_sent_at -- immediately calling
    # again with the identical `now` must NOT resend (due_for_resend is
    # false until the backoff elapses).
    again = send_due_commands(registry, gateway=gateway, now=NOW, arm_duration_s=86_400)
    assert again == []
    assert len(gateway.written) == 1


def test_a_disarm_command_is_sent_with_no_payload() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.command_disarm(3, at=NOW)
    gateway = _SpyGateway()

    sent_to = send_due_commands(registry, gateway=gateway, now=NOW, arm_duration_s=86_400)

    assert sent_to == [3]
    frame = decode(gateway.written[0])
    assert frame.msg_type == payloads.DISARM
    assert frame.payload == b""


def test_a_command_not_yet_due_is_skipped() -> None:
    """`due_for_resend()` (slice 8) backs off after the first send; a
    second call before that delay elapses must not resend.
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.command_arm(7, at=NOW)
    gateway = _SpyGateway()
    send_due_commands(registry, gateway=gateway, now=NOW, arm_duration_s=86_400)

    half_a_second_later = NOW + timedelta(milliseconds=500)
    sent_to = send_due_commands(
        registry, gateway=gateway, now=half_a_second_later, arm_duration_s=86_400
    )

    assert sent_to == []
    assert len(gateway.written) == 1


def test_a_trap_with_no_pending_command_is_skipped() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(node_id=1, state=TrapState.IDLE, at=NOW)
    gateway = _SpyGateway()

    sent_to = send_due_commands(registry, gateway=gateway, now=NOW, arm_duration_s=86_400)

    assert sent_to == []
    assert gateway.written == []


def test_a_set_targets_command_is_never_sent_from_here() -> None:
    """`domain/traps.py::Command` only ever holds a `targets_version`, never
    the tag bytes an operator actually typed -- nothing before slice 13
    persists those, so encoding a SET_TARGETS frame here would either
    resend stale bytes or send an empty list, silently clearing a trap's
    real targets. Leaving it untouched keeps `due_for_resend()` true so
    slice 13's real sender picks it up once it exists.
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.command_set_targets(9, targets_version=4, at=NOW)
    gateway = _SpyGateway()

    sent_to = send_due_commands(registry, gateway=gateway, now=NOW, arm_duration_s=86_400)

    assert sent_to == []
    assert gateway.written == []
    assert registry.trap(9).pending_command is not None
    assert registry.trap(9).pending_command.attempts == 0


def test_a_failed_write_does_not_advance_the_resend_backoff() -> None:
    """If the frame never actually left the station, `sent()` must not run
    -- otherwise `due_for_resend()` would back off for a send that never
    happened, silently delaying a command that is still, in truth, unsent.
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.command_arm(7, at=NOW)
    gateway = _SpyGateway(accept=False)

    sent_to = send_due_commands(registry, gateway=gateway, now=NOW, arm_duration_s=86_400)

    assert sent_to == []
    assert registry.trap(7).pending_command.attempts == 0
