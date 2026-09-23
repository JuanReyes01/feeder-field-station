"""``adapters/command_sender.py`` -- the last of slice 12's four orphaned
drivers: ``domain/commands.py``'s ``due_for_resend()``/``sent()`` (slice 8)
had no caller until this module. ARM and DISARM are sent for real. As of
slice 13, ``SET_TARGETS`` is sent for real too, reading back the exact tag
bytes ``app.py``'s ``PUT /targets`` route now persists -- see this module's
own docstring for what that does, and does not, prove about real delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from feeder_station.adapters.command_sender import send_due_commands
from feeder_station.domain import events, payloads
from feeder_station.domain.commands import Command
from feeder_station.domain.frames import decode
from feeder_station.domain.traps import Registry, TrapState

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _pending(registry: Registry, node_id: int) -> Command:
    """`Trap.pending_command` is `Command | None` -- this narrows it once
    per call site instead of repeating an `assert ... is not None` mypy
    cannot carry across two separate `registry.trap(node_id)` calls.
    """
    command = registry.trap(node_id).pending_command
    assert command is not None
    return command


class _SpyGateway:
    def __init__(self, *, accept: bool = True) -> None:
        self.written: list[bytes] = []
        self._accept = accept

    def write_frame(self, raw_frame: bytes) -> bool:
        if self._accept:
            self.written.append(raw_frame)
        return self._accept


@dataclass
class _FakeRow:
    """The one shape `send_due_commands` reads off a stored row: its
    `fields` -- a narrow double for `adapters/store.py::StoredEvent`, the
    same "Protocol fake" discipline DD16 already applies one layer down.
    """

    fields: dict[str, object]


class _FakeStore:
    """A narrow double for `adapters/store.py::Store` -- ARM/DISARM tests
    never touch it; only the SET_TARGETS tests below seed a row.
    """

    def __init__(self, rows: dict[tuple[int, int], dict[str, object]] | None = None) -> None:
        self._rows = rows or {}

    def latest(self, node_id: int, msg_type: int) -> _FakeRow | None:
        fields = self._rows.get((node_id, msg_type))
        return None if fields is None else _FakeRow(fields)


def _set_targets_header(frame: bytes) -> tuple[int, int, int, int]:
    targets_version = frame[0] | (frame[1] << 8)
    seq = frame[2] | (frame[3] << 8)
    seq_total = frame[4] | (frame[5] << 8)
    count = frame[6]
    return targets_version, seq, seq_total, count


def _set_targets_store(node_id: int, *, targets_version: int, tags: list[bytes]) -> _FakeStore:
    return _FakeStore(
        {
            (node_id, events.OPERATOR_SET_TARGETS): {
                "targets_version": targets_version,
                "tags": [tag.hex() for tag in tags],
            }
        }
    )


# --- a fresh ARM command is due immediately, and is sent for real ----------


def test_a_never_sent_arm_command_is_sent_and_marked() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.command_arm(7, at=NOW)
    gateway = _SpyGateway()
    store = _FakeStore()

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400
    )

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
    again = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400
    )
    assert again == []
    assert len(gateway.written) == 1


def test_a_disarm_command_is_sent_with_no_payload() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.command_disarm(3, at=NOW)
    gateway = _SpyGateway()
    store = _FakeStore()

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400
    )

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
    store = _FakeStore()
    send_due_commands(registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400)

    half_a_second_later = NOW + timedelta(milliseconds=500)
    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=half_a_second_later, arm_duration_s=86_400
    )

    assert sent_to == []
    assert len(gateway.written) == 1


def test_a_trap_with_no_pending_command_is_skipped() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(node_id=1, state=TrapState.IDLE, at=NOW)
    gateway = _SpyGateway()
    store = _FakeStore()

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400
    )

    assert sent_to == []
    assert gateway.written == []


def test_a_failed_write_does_not_advance_the_resend_backoff() -> None:
    """If the frame never actually left the station, `sent()` must not run
    -- otherwise `due_for_resend()` would back off for a send that never
    happened, silently delaying a command that is still, in truth, unsent.
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.command_arm(7, at=NOW)
    gateway = _SpyGateway(accept=False)
    store = _FakeStore()

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400
    )

    assert sent_to == []
    assert _pending(registry, 7).attempts == 0


# --- 13.7/13.8: SET_TARGETS is sent for real, from stored content ----------


def test_set_targets_frames_carry_the_stored_tags() -> None:
    """trap-commands spec.md: the sender must emit real chunked frames from
    the operator's own stored content through `encode_set_targets`, not an
    empty list -- `domain/traps.py::Command` itself only ever carries a
    version number (slice 8), never the bytes; those come from the store
    row `app.py`'s `PUT /targets` route wrote (13.5/13.6).
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    tags = [bytes([0x01, 0, 0, 0, 0, 0, 0]), bytes([0x02, 0, 0, 0, 0, 0, 0])]
    registry.command_set_targets(9, targets_version=4, at=NOW, tags=tags)
    store = _set_targets_store(9, targets_version=4, tags=tags)
    gateway = _SpyGateway()

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400
    )

    assert sent_to == [9]
    assert len(gateway.written) == 1
    frame = decode(gateway.written[0])
    assert frame.msg_type == payloads.SET_TARGETS
    assert frame.node_id == 9
    version, seq, seq_total, count = _set_targets_header(frame.payload)
    assert (version, seq, seq_total, count) == (4, 0, 1, 2)
    assert frame.payload[7:] == b"".join(tags)
    # sent() must have advanced attempts -- a real send happened.
    assert _pending(registry, 9).attempts == 1


def test_set_targets_with_nothing_stored_yet_is_not_sent() -> None:
    """A pending command can exist in the registry (e.g. after a restart
    that rehydrated only the version, not yet the bytes) with no matching
    store row -- nothing durable to send, so nothing goes out, and the
    backoff must not advance for a send that did not happen.
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.command_set_targets(9, targets_version=4, at=NOW)
    store = _FakeStore()
    gateway = _SpyGateway()

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400
    )

    assert sent_to == []
    assert gateway.written == []
    assert _pending(registry, 9).attempts == 0


def test_a_failed_set_targets_write_does_not_advance_the_backoff() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    tags = [bytes([0x01, 0, 0, 0, 0, 0, 0])]
    registry.command_set_targets(9, targets_version=4, at=NOW, tags=tags)
    store = _set_targets_store(9, targets_version=4, tags=tags)
    gateway = _SpyGateway(accept=False)

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400
    )

    assert sent_to == []
    assert _pending(registry, 9).attempts == 0


# --- 13.9/13.10: a split list is sent whole, and delivered from the version
# --- match alone, exactly as domain/traps.py's own model already requires -


def test_a_list_longer_than_one_frame_is_delivered_whole() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    tags = [bytes([i, 0, 0, 0, 0, 0, 0]) for i in range(40)]  # > one frame's 27-tag limit
    registry.command_set_targets(9, targets_version=9, at=NOW, tags=tags)
    store = _set_targets_store(9, targets_version=9, tags=tags)
    gateway = _SpyGateway()

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400
    )

    assert sent_to == [9]
    assert len(gateway.written) == 2  # both frames of the split list went out together
    decoded = [decode(raw) for raw in gateway.written]
    assert {f.msg_type for f in decoded} == {payloads.SET_TARGETS}
    seqs = sorted(_set_targets_header(f.payload)[1] for f in decoded)
    assert seqs == [0, 1]
    # S2: still pending -- no HEARTBEAT has reported the new version yet.
    assert registry.trap(9).pending_command is not None

    registry.heartbeat(
        node_id=9, state=TrapState.ARMED, targets_version=9, at=NOW + timedelta(seconds=5)
    )
    # Delivered from the version match alone -- the station never tracked
    # which half of the split list arrived separately.
    assert registry.trap(9).pending_command is None
