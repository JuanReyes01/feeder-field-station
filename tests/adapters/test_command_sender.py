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


class _AllowAllBudget:
    """A `TransmitBudget` double that never refuses -- every test in this
    file except the CRITICAL 3 ones below is about command-sending
    correctness, not the shared station-wide budget; this keeps them
    decoupled from it, the same way `heartbeat_interval` is overridden in
    `tests/adapters/test_workers.py` to decouple pacing tests from
    unrelated wiring tests.
    """

    def try_send(self) -> bool:
        return True

    def try_send_n(self, n: int) -> bool:
        return True


class _TokenLedgerBudget:
    """A `TransmitBudget` double that records exactly how many tokens each
    call actually asks for, mirroring the real `SharedBudget`/`TokenBucket`
    pair's own atomic accounting: `try_send()` is a request for one token,
    `try_send_n(n)` a request for `n`, both drawn from the same running
    balance, all-or-nothing -- never a partial spend.

    Built for BEHAVIOUR 1 (verify-report-final.md, sdd-verify
    field-station-p4): the old `_send_set_targets` asked for one token
    per trap regardless of `SET_TARGETS`'s own chunk count, so a ten-chunk
    split list spent one token for ten frames on the wire -- the exact
    bypass that let one operator action restore the pre-fix airtime
    occupancy this whole remediation exists to close.
    """

    def __init__(self, capacity: int) -> None:
        self.remaining = capacity
        self.charged: list[int] = []

    def try_send(self) -> bool:
        return self.try_send_n(1)

    def try_send_n(self, n: int) -> bool:
        if n > self.remaining:
            return False
        self.remaining -= n
        self.charged.append(n)
        return True


class _CountingBudget:
    """A `TransmitBudget` double that allows exactly `capacity` sends, then
    refuses -- proves `send_due_commands` checks the budget once per trap
    actually attempted, not once per call (CRITICAL 3, sdd-verify
    field-station-p4): a single call sweeping ten due commands must not
    write more than the budget allows in that one pass.
    """

    def __init__(self, capacity: int) -> None:
        self._remaining = capacity
        self.calls = 0

    def try_send(self) -> bool:
        self.calls += 1
        if self._remaining <= 0:
            return False
        self._remaining -= 1
        return True

    def try_send_n(self, n: int) -> bool:
        self.calls += 1
        if self._remaining < n:
            return False
        self._remaining -= n
        return True


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
        registry,
        store=store,
        gateway=gateway,
        now=NOW,
        arm_duration_s=86_400,
        budget=_AllowAllBudget(),
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
        registry,
        store=store,
        gateway=gateway,
        now=NOW,
        arm_duration_s=86_400,
        budget=_AllowAllBudget(),
    )
    assert again == []
    assert len(gateway.written) == 1


def test_a_disarm_command_is_sent_with_no_payload() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.command_disarm(3, at=NOW)
    gateway = _SpyGateway()
    store = _FakeStore()

    sent_to = send_due_commands(
        registry,
        store=store,
        gateway=gateway,
        now=NOW,
        arm_duration_s=86_400,
        budget=_AllowAllBudget(),
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
    send_due_commands(
        registry,
        store=store,
        gateway=gateway,
        now=NOW,
        arm_duration_s=86_400,
        budget=_AllowAllBudget(),
    )

    half_a_second_later = NOW + timedelta(milliseconds=500)
    sent_to = send_due_commands(
        registry,
        store=store,
        gateway=gateway,
        now=half_a_second_later,
        arm_duration_s=86_400,
        budget=_AllowAllBudget(),
    )

    assert sent_to == []
    assert len(gateway.written) == 1


def test_a_trap_with_no_pending_command_is_skipped() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(node_id=1, state=TrapState.IDLE, at=NOW)
    gateway = _SpyGateway()
    store = _FakeStore()

    sent_to = send_due_commands(
        registry,
        store=store,
        gateway=gateway,
        now=NOW,
        arm_duration_s=86_400,
        budget=_AllowAllBudget(),
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
        registry,
        store=store,
        gateway=gateway,
        now=NOW,
        arm_duration_s=86_400,
        budget=_AllowAllBudget(),
    )

    assert sent_to == []
    assert _pending(registry, 7).attempts == 0


# --- docs/pending.md, "A command the node refuses is retried forever" -----
#
# The bench measurement this fix answers: one `POST /arm` sent hours
# earlier produced nine `FAULT` rows and was still being resent -- each
# retry a frame the shared station-wide budget (CRITICAL 3) spent on a
# command the node had already, explicitly, refused, starving `TIME_SYNC`
# of the turns the round-robin scheduler (`adapters/workers.py`) would
# otherwise have given it. Cancelling the command in `Registry.fault()`
# is what stops it: `send_due_commands` only ever resends a trap's
# *pending* command, so once it is `None` there is nothing left for this
# function to find, spend a token on, or write to the gateway.


def test_a_fault_cancelled_command_is_never_resent_and_spends_no_budget() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.command_arm(7, at=NOW)
    registry.fault(7, at=NOW + timedelta(seconds=5), code=5)  # ARM_REQUIRES_PRESENCE
    gateway = _SpyGateway()
    budget = _TokenLedgerBudget(capacity=10)

    sent_to = send_due_commands(
        registry,
        store=_FakeStore(),
        gateway=gateway,
        now=NOW + timedelta(minutes=10),
        arm_duration_s=86_400,
        budget=budget,
    )

    assert sent_to == []
    assert gateway.written == []
    assert budget.charged == []  # not one token spent -- the airtime the bench measured lost


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
        registry,
        store=store,
        gateway=gateway,
        now=NOW,
        arm_duration_s=86_400,
        budget=_AllowAllBudget(),
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
        registry,
        store=store,
        gateway=gateway,
        now=NOW,
        arm_duration_s=86_400,
        budget=_AllowAllBudget(),
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
        registry,
        store=store,
        gateway=gateway,
        now=NOW,
        arm_duration_s=86_400,
        budget=_AllowAllBudget(),
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
        registry,
        store=store,
        gateway=gateway,
        now=NOW,
        arm_duration_s=86_400,
        budget=_AllowAllBudget(),
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


# --- BEHAVIOUR 1 (verify-report-final.md, sdd-verify field-station-p4): a
# --- chunked SET_TARGETS must charge N tokens for its N frames, atomically,
# --- never one flat token regardless of chunk count -----------------------


def test_a_chunked_set_targets_send_charges_n_tokens_for_n_frames_not_one() -> None:
    """BEHAVIOUR 1: the only other split-list test above uses
    `_AllowAllBudget()`, which is exactly why the one-token-for-N-frames
    bypass survived -- it never inspects what the budget itself was asked
    for. A 40-tag list needs two `SET_TARGETS` frames
    (`TAGS_PER_SET_TARGETS_FRAME` is 27); the charge for sending both must
    be `[2]`, one atomic two-token spend, never `[1]`.
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    tags = [bytes([i, 0, 0, 0, 0, 0, 0]) for i in range(40)]  # 2 chunks
    registry.command_set_targets(9, targets_version=9, at=NOW, tags=tags)
    store = _set_targets_store(9, targets_version=9, tags=tags)
    gateway = _SpyGateway()
    budget = _TokenLedgerBudget(capacity=10)

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400, budget=budget
    )

    assert sent_to == [9]
    assert len(gateway.written) == 2  # both chunks of the split list went out together
    assert budget.charged == [2], (
        f"charged {budget.charged} token(s) for a 2-chunk send -- must be exactly [2], one "
        "atomic N-token charge, never a flat charge of 1 regardless of chunk count"
    )


def test_a_chunked_set_targets_send_is_refused_whole_when_fewer_than_n_tokens_remain() -> None:
    """The other half of BEHAVIOUR 1's fix: refusing an N-chunk send must
    write *no* chunk at all when fewer than N tokens are available.
    trap-commands spec.md's own delivery model already forbids the station
    from tracking which half of a split list arrived -- a partial charge
    here would let the budget layer split a send the frame layer itself
    refuses to split, and the trap must stay pending, unattempted, so a
    later sweep (once the budget allows) offers the whole list again.
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    tags = [bytes([i, 0, 0, 0, 0, 0, 0]) for i in range(40)]  # 2 chunks
    registry.command_set_targets(9, targets_version=9, at=NOW, tags=tags)
    store = _set_targets_store(9, targets_version=9, tags=tags)
    gateway = _SpyGateway()
    budget = _TokenLedgerBudget(capacity=1)  # only 1 token -- the send needs 2

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400, budget=budget
    )

    assert sent_to == []
    assert gateway.written == []  # neither chunk went out -- no partial send
    assert budget.remaining == 1  # nothing spent on the refused attempt
    assert budget.charged == []
    assert _pending(registry, 9).attempts == 0  # stays due, retried next sweep


# --- CRITICAL 3 (sdd-verify field-station-p4): a shared budget bounds a
# --- single sweep, not just how often the sweep itself is called ----------


def test_a_shared_budget_caps_how_many_due_commands_one_sweep_sends() -> None:
    """verify-report-2.md, CRITICAL 3: ten traps holding an unconfirmed ARM
    measured 0.3158 frames/s -- 11.68% airtime occupancy against a ~1%
    budget -- because `send_due_commands` had no pacing of its own beyond
    each command's individual backoff, and a single call iterates every
    due trap in one pass. Gating the call frequency alone (e.g. only
    calling this function once per budget refill) would not have been
    enough: this proves the budget is spent per trap *inside* one sweep,
    the only granularity that actually bounds a ten-trap burst.
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    for node_id in range(10):
        registry.command_arm(node_id, at=NOW)
    gateway = _SpyGateway()
    store = _FakeStore()
    budget = _CountingBudget(capacity=3)

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400, budget=budget
    )

    assert len(sent_to) == 3
    assert len(gateway.written) == 3
    # The seven refused traps must stay pending and unattempted -- sent()
    # never ran for them, so due_for_resend() re-offers them next sweep
    # instead of silently losing seven of the ten ARM commands.
    for node_id in range(10):
        if node_id not in sent_to:
            assert registry.trap(node_id).pending_command is not None
            assert registry.trap(node_id).pending_command.attempts == 0  # type: ignore[union-attr]


# --- GET_TARGETS (0x87, docs/protocol/messages.md) -------------------------


def test_a_pending_targets_fetch_is_sent_as_an_empty_get_targets_frame() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, targets_version=3, at=NOW)
    assert registry.trap(7).pending_targets_fetch is not None  # armed: no reported list yet
    gateway = _SpyGateway()
    store = _FakeStore()

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400, budget=_AllowAllBudget()
    )

    assert sent_to == [7]
    assert len(gateway.written) == 1
    frame = decode(gateway.written[0])
    assert frame.msg_type == payloads.GET_TARGETS
    assert frame.node_id == 7
    assert frame.payload == b""
    assert registry.trap(7).pending_targets_fetch.attempts == 1  # type: ignore[union-attr]


def test_a_targets_fetch_not_yet_due_is_skipped() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, targets_version=3, at=NOW)
    gateway = _SpyGateway()
    store = _FakeStore()
    send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400, budget=_AllowAllBudget()
    )

    half_a_second_later = NOW + timedelta(milliseconds=500)
    sent_to = send_due_commands(
        registry,
        store=store,
        gateway=gateway,
        now=half_a_second_later,
        arm_duration_s=86_400,
        budget=_AllowAllBudget(),
    )

    assert sent_to == []
    assert len(gateway.written) == 1


def test_a_trap_with_no_pending_targets_fetch_is_skipped() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, targets_version=3, at=NOW)
    registry.reported_targets_chunk(7, targets_version=3, seq=0, seq_total=1, tags=[])
    assert registry.trap(7).pending_targets_fetch is None  # settled -- versions agree
    gateway = _SpyGateway()
    store = _FakeStore()

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400, budget=_AllowAllBudget()
    )

    assert sent_to == []
    assert gateway.written == []


def test_a_failed_write_leaves_the_targets_fetch_pending_and_unattempted() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, targets_version=3, at=NOW)
    gateway = _SpyGateway(accept=False)
    store = _FakeStore()

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400, budget=_AllowAllBudget()
    )

    assert sent_to == []
    fetch = registry.trap(7).pending_targets_fetch
    assert fetch is not None
    assert fetch.attempts == 0  # a dropped write must not look like a send that happened


def test_a_refused_budget_leaves_the_targets_fetch_pending_and_unattempted() -> None:
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, targets_version=3, at=NOW)
    gateway = _SpyGateway()
    store = _FakeStore()
    budget = _CountingBudget(capacity=0)

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400, budget=budget
    )

    assert sent_to == []
    assert gateway.written == []
    fetch = registry.trap(7).pending_targets_fetch
    assert fetch is not None
    assert fetch.attempts == 0


def test_a_pending_command_and_a_pending_targets_fetch_are_both_sent_in_one_sweep() -> None:
    """`Trap.pending_command` (an operator's intent) and `Trap.
    pending_targets_fetch` (the station's own automatic reconciliation) are
    unrelated facts -- both due for the same trap at the same tick must
    both go out, not one starving the other.
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, targets_version=3, at=NOW)
    registry.command_disarm(7, at=NOW)
    gateway = _SpyGateway()
    store = _FakeStore()

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400, budget=_AllowAllBudget()
    )

    assert sent_to == [7]  # one node, two frames
    assert len(gateway.written) == 2
    msg_types = {decode(frame).msg_type for frame in gateway.written}
    assert msg_types == {payloads.DISARM, payloads.GET_TARGETS}


def test_a_targets_fetch_is_dispatched_for_a_trap_with_no_pending_command_at_all() -> None:
    """The independent-dispatch guarantee, the other direction: a trap with
    nothing pending except an automatic fetch must still be visited, not
    skipped because ``trap.pending_command`` alone is ``None``.
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, targets_version=3, at=NOW)
    assert registry.trap(7).pending_command is None
    gateway = _SpyGateway()
    store = _FakeStore()

    sent_to = send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400, budget=_AllowAllBudget()
    )

    assert sent_to == [7]
    assert decode(gateway.written[0]).msg_type == payloads.GET_TARGETS


def test_repeated_sweeps_resend_a_targets_fetch_on_its_own_backoff_schedule() -> None:
    """`PendingTargetsFetch.due_for_resend` reuses `RESEND_BACKOFF_S`'s own
    schedule -- proven end to end here the same way ARM/DISARM's resend
    already is above.
    """
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.heartbeat(node_id=7, state=TrapState.ARMED, targets_version=3, at=NOW)
    gateway = _SpyGateway()
    store = _FakeStore()

    send_due_commands(
        registry, store=store, gateway=gateway, now=NOW, arm_duration_s=86_400, budget=_AllowAllBudget()
    )
    assert len(gateway.written) == 1

    one_second_later = NOW + timedelta(seconds=1)
    sent_to = send_due_commands(
        registry,
        store=store,
        gateway=gateway,
        now=one_second_later,
        arm_duration_s=86_400,
        budget=_AllowAllBudget(),
    )

    assert sent_to == [7]
    assert len(gateway.written) == 2
