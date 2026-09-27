"""Starts and supervises the two background threads design.md section 7
names (the "gateway thread" and the "scheduler thread — 1 s tick"), and
wires together the four drivers earlier slices left fully tested with
nothing calling them:

1. the serial gateway reader (slice 5, ``adapters/serial_gateway.py``),
2. backlog rotation (slice 7, ``adapters/scheduler.py::run_rotation_round``),
3. the command sender (slice 8's model, wired here via
   ``adapters/command_sender.py::send_due_commands``),
4. clock trust's own sender (slice 9,
   ``adapters/clock.py::attempt_time_sync``).

**Nothing before this slice ever started a thread** — see this change's own
tasks.md, slice 12: the whole change could previously finish green with a
station that serves HTTP and never speaks to a trap. ``app.py``'s lifespan
is this module's only caller.

**The registry lock, named in design.md's own concurrency table** ("One
``threading.Lock``, owned by the shell... because ``domain/`` stays free of
concurrency primitives, exactly as it stays free of clocks"): before this
slice, exactly one thread — the HTTP threadpool — ever touched
``Registry``. This slice gives it two more callers, so the lock this
module and ``app.py`` both accept as ``registry_lock`` stops being
theoretical.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from feeder_station.adapters.clock import Clock, attempt_time_sync
from feeder_station.adapters.command_sender import send_due_commands
from feeder_station.adapters.ingest import Publish, ingest_frame
from feeder_station.adapters.scheduler import poll_node
from feeder_station.adapters.store import Store
from feeder_station.config import StationConfig
from feeder_station.domain import backlog, pacing, payloads
from feeder_station.domain.clock_trust import may_distribute_time, node_clock_is_trusted
from feeder_station.domain.traps import Registry


class FrameGateway(Protocol):
    """The one surface a transport (``SerialGateway``, one port) gives this
    module -- narrow on purpose, the same "only what this module actually
    calls" discipline ``adapters/ingest.py``'s own ``AckPort`` and
    ``adapters/command_sender.py``'s own ``CommandGateway`` already apply
    one layer down. Structural (``Protocol``), not a shared base class: a
    concrete gateway needs to import neither this module nor any sibling
    gateway to satisfy it.

    A second transport, a listening TCP socket nodes dialed into over
    Wi-Fi, briefly existed here (``adapters/network_gateway.py``) and was
    removed: an ESP32 that associates with a Wi-Fi AP has its radio
    channel dragged to the AP's own channel (Espressif's "home channel"
    rule), the same radio ESP-NOW needs, and the site's Starlink network
    gives this station no control over that channel. The permanent
    architecture stays trap --ESP-NOW--> gateway ESP32 --USB serial-->
    station, i.e. ``SerialGateway`` alone.
    """

    def ensure_connected(self, *, stop: Callable[[], bool] = ...) -> bool: ...

    def read_frames(self, *, size: int = 4096) -> list[bytes]: ...

    def write_frame(self, raw_frame: bytes) -> bool: ...

    def record_payload_error(self) -> None: ...


# CRITICAL 3 (sdd-verify field-station-p4): the three orderings
# `run_scheduler_loop` cycles through, one per tick, so no single
# transmitter permanently wins every tie for the shared budget -- see that
# function's own docstring for why a fixed priority (e.g. commands always
# first) was rejected.
_ROTATIONS: tuple[tuple[str, str, str], ...] = (
    ("commands", "time_sync", "poll"),
    ("time_sync", "poll", "commands"),
    ("poll", "commands", "time_sync"),
)


@dataclass
class SharedBudget:
    """Adapter-level mutable holder around the pure ``domain/pacing.py::
    TokenBucket`` (CRITICAL 3, sdd-verify field-station-p4) -- the same
    "pure state, mutated under registry_lock" shape this module already
    gives ``backlog.RotationSchedule``, just wrapped so every transmitter
    this loop calls sees one small, required capability
    (``try_send() -> bool``) instead of the bucket's own immutable-replace
    API. Driven by ``time.monotonic()``, never by the injected ``Clock``:
    airtime occupancy is a real, physical constraint on elapsed wall-clock
    time, orthogonal to the station's notion of *trusted* time (which
    several tests, and a station with no NTP yet, deliberately freeze or
    distrust) -- binding the budget's refill to that clock would make it
    stop refilling exactly when the clock is least reliable.

    One instance, constructed once per ``run_scheduler_loop`` call and
    passed by reference into every transmitter it calls -- never
    reconstructed per call, which would silently reset the budget and
    defeat the whole fix (the same "a fresh copy per call is a bug, not a
    feature" lesson ``RotationSchedule`` already needed for the rotation
    itself, CRITICAL 4: rebuilding it every tick was the original defect).
    """

    bucket: pacing.TokenBucket

    def try_send(self) -> bool:
        self.bucket, allowed = pacing.try_consume(self.bucket, now=time.monotonic())
        return allowed

    def try_send_n(self, n: int) -> bool:
        """BEHAVIOUR 1 (verify-report-final.md, sdd-verify field-station-p4):
        the one call a multi-frame send (``adapters/command_sender.py``'s
        chunked ``SET_TARGETS``) needs -- an atomic charge of ``n`` tokens
        for its ``n`` frames, never the flat one-token charge ``try_send()``
        gives a single-frame caller.
        """
        self.bucket, allowed = pacing.try_consume_n(self.bucket, n, now=time.monotonic())
        return allowed


@dataclass
class WorkerStatus:
    """What ``/health`` reads about one background thread (12.0c). Actual
    aliveness is read straight off the ``threading.Thread`` object itself
    (the single source of truth); this only ever records the reason a
    thread stopped running on its own, which ``thread.is_alive()`` alone
    cannot say.
    """

    last_error: str | None = None


def run_supervised(status: WorkerStatus, body: Callable[[], None]) -> None:
    """Run ``body`` (a worker loop), catching anything it raises instead of
    letting it vanish silently off a daemon thread.

    This is the whole of 12.0c: without it, a bug that kills the gateway
    thread in week three leaves the station serving HTTP perfectly, every
    trap looking unreachable, and nothing anywhere saying why — see this
    slice's own tasks.md for that exact scenario.
    """
    try:
        body()
    except BaseException as exc:  # noqa: BLE001 -- deliberately broad: ANY escape
        # must be recorded, not only the exception types this module
        # happens to anticipate today.
        status.last_error = f"{type(exc).__name__}: {exc}"


def run_gateway_loop(
    gateway: FrameGateway,
    *,
    store: Store,
    registry: Registry,
    registry_lock: threading.Lock,
    publish: Publish,
    clock: Clock,
    stop: threading.Event,
    last_frame_arrival: dict[int, float],
    record_payload_error: Callable[[], None],
) -> None:
    """The blocking serial-read half of design.md section 7's "gateway
    thread": ``SerialGateway.ensure_connected()``/``read_frames()`` (slice
    5) feeding ``ingest_frame()`` (slice 6's own S9 ordering) — wired
    together, for the first time, by this slice.

    Ends when ``stop`` is set. ``ensure_connected``'s own ``stop`` polling
    is what keeps a stuck reconnect loop responsive to shutdown instead of
    blocking it (design.md section 7's "Shutdown" row): it is checked
    before every connect attempt, so nothing here can be caught mid-write
    to a store that is about to be closed underneath it — the reconnect
    backoff sleep is the only part shutdown may have to outlast, and it
    never touches the store while doing so.

    ``last_frame_arrival`` (CRITICAL 4, sdd-verify field-station-p4): a
    plain ``dict[node_id, received_epoch]``, the same "plain dict mutated
    from three threads, guarded by ``registry_lock``" shape design.md
    section 7 already gives ``Registry`` itself. Populated here, under the
    same lock ``ingest_frame`` already runs inside, so ``run_scheduler_
    loop``'s own read of it on the scheduler thread never races this
    write. This is the "record, on the gateway thread, the last tick a
    frame arrived per node" fact that loop's own ``responded`` check now
    compares against, replacing the unsound 15-minute-window heuristic
    (``_recently_reachable``, deleted) this slice's own verify report
    flagged as the direct cause of the scheduler's radio-saturating retry
    behaviour (CRITICAL 4).
    """
    while not stop.is_set():
        if not gateway.ensure_connected(stop=stop.is_set):
            return  # stop was set while still reconnecting -- a clean exit
        for raw_frame in gateway.read_frames():
            with registry_lock:
                ingest_frame(
                    raw_frame,
                    store=store,
                    ack_port=gateway,
                    registry=registry,
                    publish=publish,
                    received_epoch=clock.now().timestamp(),
                    clock_trust=clock.trust().value,
                    record_arrival=last_frame_arrival.__setitem__,
                    record_payload_error=record_payload_error,
                )


def run_scheduler_loop(
    *,
    store: Store,
    gateway: FrameGateway,
    registry: Registry,
    registry_lock: threading.Lock,
    clock: Clock,
    config: StationConfig,
    stop: threading.Event,
    tick_s: float = 1.0,
    last_frame_arrival: dict[int, float] | None = None,
) -> None:
    """design.md section 7's "scheduler thread — 1 s tick": backlog
    rotation, command retries, and TIME_SYNC — the three drivers slices 7,
    8 and 9 left fully tested with nothing calling them, on the tick this
    slice finally starts.

    ``stop.wait(tick_s)`` — not ``time.sleep`` — is what makes shutdown
    interrupt a long tick immediately rather than waiting it out.

    **CRITICAL 4 fix (sdd-verify, field-station-p4), replacing this
    method's old per-tick full-sweep body wholesale:** the old body called
    ``adapters/scheduler.py::run_rotation_round`` fresh every 1 s tick,
    which itself rebuilds ``backlog.start()`` and sweeps the *whole*
    roster — including up to ``retry_budget`` retries per unresponsive
    node — to completion before returning. Measured: 13.3-40 GET_BACKLOG
    frames/s at ten known traps, against docs/protocol/messages.md's own
    "keep the channel under 1% occupied" budget (a ~50-byte frame costs
    0.37 s of airtime at Long Fast, so 1% is ≈0.027 frames/s — the station
    was transmitting 400-1500x over budget, continuously). Two changes:

    1. **The rotation persists across ticks.** One
       ``domain/backlog.py::RotationSchedule`` lives in this loop's own
       local scope and is only rebuilt when the roster itself changes —
       never every tick — so "whose turn is next" and the in-progress
       retry count both survive between calls, as design.md's own
       round-robin model requires.
    2. **Paced at one GET_BACKLOG poll per ``config.heartbeat_interval``**
       (this fix's own resolution of design's "advance it by one node per
       tick (or per heartbeat_interval)" — per *tick* alone, at the
       default 1 s, is still ≈37% occupancy for one frame; per
       ``heartbeat_interval``, 5 minutes by default, is ≈0.12% regardless
       of roster size, comfortably inside budget however many traps are
       configured). ``tests/adapters/test_workers.py`` measures the
       resulting real rate and asserts the default holds the budget.
       **CRITICAL 5 (sdd-verify field-station-p4, batch 4):**
       ``run_rotation_round`` had zero production callers and is deleted
       outright, not kept "untouched" as an earlier revision of this
       docstring claimed; its poll-order coverage now targets this loop
       directly (``tests/adapters/test_workers.py``'s own poll-order test).

    ``responded``, for ``domain/backlog.py::record_response``'s own retry
    accounting, is now "did this node send *any* frame since the last time
    this loop polled it" (``last_frame_arrival``, populated by
    ``run_gateway_loop`` on the gateway thread) — replacing
    ``_recently_reachable``'s 15-minute reachability window, which
    answered a different question (S11's own staleness fact) and, for a
    genuinely silent node, always returned ``False`` — the direct cause of
    the retry budget being spent on every single tick. Because a
    ``GET_BACKLOG`` reply is asynchronous (still true, and still this
    loop's own honest limit — the docstring this replaces already named
    it), this can only ever answer for the *previous* poll of a node by
    the time its next turn comes up, never the poll about to be sent.

    **The clock-trust bug fix, replacing this loop's own ``synced_node_ids``
    wholesale:** TIME_SYNC retry used to be gated on a *station-side* set —
    attempted once per node id, the first tick that id appeared in the
    roster, and never again once ``attempt_time_sync`` returned ``True`` —
    on the theory that a returned "sent" meant the node now had a clock.
    It did not: a bench day with the station restarted about five times,
    the node rebooting eighteen times and the gateway going deaf at least
    once left ``clock_epoch: 0``/``clock_source: 0`` (unset) across 2,685
    heartbeats, because ``attempt_time_sync``'s ``True`` only ever meant
    "a frame left this station's own serial port", never "the node applied
    it" — a dropped, delayed or pre-reboot send counted as done exactly as
    permanently as a delivered one, and nothing here ever asked the node
    whether it actually was.

    This is S4 again, applied to the clock where it was never applied
    before: ``Trap.pending_command`` clears only on the trap's own next
    ``HEARTBEAT`` confirming it, never merely on a frame having been
    written — see ``domain/traps.py``'s own module docstring. The station
    already receives, and already durably stores, the evidence this fix
    runs on: every ``HEARTBEAT`` carries ``clock_source`` (docs/protocol/
    messages.md: 0 unset, 1 RTC), read back here straight off the node's
    own latest stored row (``store.latest(node_id, payloads.HEARTBEAT)``)
    rather than duplicated onto ``Trap`` — this fact needs no in-memory
    home of its own; the store is already the durable copy, and rereading
    it once per cadence (below) costs nothing next to the fsync'd write a
    withheld attempt already makes at the same rate. A node is due for
    ``TIME_SYNC`` for as long as its own last-reported ``clock_source``
    says untrusted (``domain.clock_trust.node_clock_is_trusted``, ``None``
    included — no heartbeat stored yet is not "trusted"), and stops being
    due the moment the node itself reports one, not before. **CRITICAL 3
    stands unchanged:** a *withheld* attempt (clock still ``unset``
    station-side) still never counts as anything sent, below.

    A retry this drives off reported fact, not a one-shot flag, still
    needs its own pacing so a stubbornly-unsynced node cannot compete for
    the shared budget every single tick: ``last_time_sync_attempt_at``
    gates a real (would-send) attempt to once per
    ``config.heartbeat_interval``, the same cadence — and the same
    reasoning — ``last_withheld_attempt_at`` below already applies to a
    withheld one. A node refused by the budget itself is not charged
    against that cadence: the attempt did not happen, so it stays due
    immediately, the same "refused stays due, unattempted" rule every
    other transmitter in this loop already follows.

    **CRITICAL 3 fix (sdd-verify field-station-p4, batch 3a):** the three
    transmitters above -- ``poll_node``, ``send_due_commands`` and
    ``attempt_time_sync``'s *actual* sends -- now all draw from one
    ``SharedBudget`` (``domain/pacing.py``), sized from
    ``config.channel_budget_frames_per_s``. Before this fix, only
    ``poll_node`` was paced; ten traps holding an unconfirmed command
    alone drove the station to 11.68% real airtime occupancy against the
    ~1% ``docs/protocol/messages.md`` budget, because ``send_due_commands``
    and ``attempt_time_sync`` each had their own unpaced loop and nothing
    bounded the *sum*.

    ``poll_node`` and ``attempt_time_sync`` are gated here, at their one
    production call site each (confirmed by ``rg 'poll_node\\(|attempt_
    time_sync\\('`` across ``src/`` -- ``poll_node``'s only caller now
    that ``run_rotation_round`` is deleted, not merely uncalled, CRITICAL
    5): a refused attempt simply is not made this tick, and (for the poll)
    ``next_poll_due_at`` is left unadvanced so the same turn is retried,
    not lost. ``send_due_commands`` is gated
    *inside* itself instead, because one call can sweep every trap in the
    roster -- a call-site check alone would not have bounded a ten-trap
    burst in a single pass. TIME_SYNC only spends a token when a send
    would actually happen (``may_distribute_time(clock.trust())``); a
    withheld attempt still costs nothing, exactly as before this fix --
    CRITICAL 4's own write-volume problem is a separate, later batch.

    **Scheduler-throttle fix (bench repro, docs/pending.md):** raising
    ``--channel-budget-frames-per-s`` past the default used to hang the
    whole station -- one ``/health`` succeeded, then nothing responded and
    no event was ingested again. The reported mechanism: ``poll_node``'s
    and ``attempt_time_sync``'s calls into ``gateway.write_frame()`` used
    to run *inside* the same ``with registry_lock:`` block that reads
    ``registry.traps()`` -- yet neither call touches ``Registry`` or any
    ``Trap`` at all (confirmed by their own signatures: no ``registry``
    parameter). A real ``SerialGateway`` write has no timeout
    (``adapters/serial_gateway.py::_default_open_port``, pyserial's own
    ``write_timeout=None`` default): at the near-zero default rate, a
    stalled far end never fills the OS's own TX buffer inside any
    realistic observation window, but a high budget keeps every tick's
    burst full continuously, and the first write that then blocks on a
    full buffer blocks *forever* -- holding ``registry_lock`` the whole
    time and starving every HTTP handler and the gateway thread's own
    ``ingest_frame`` call (CRITICAL 1), which take the same lock. The
    budget was silently doubling as the loop's own throttle, which it must
    never be: it decides what may be *sent*, never how often -- or how
    long -- the loop holds a lock other threads need.

    Fixed by no longer taking the lock for either call: both now run after
    the tick's own lock-protected reads (``node_ids`` up front, and, for
    ``poll``, the single ``last_frame_arrival`` lookup it needs) are
    already done. ``send_due_commands`` is the one category still
    dispatched under the lock: unlike the other two, it genuinely reads
    and writes ``Trap.pending_command``, shared with the gateway thread's
    own heartbeat handling (``Registry.heartbeat``) -- splitting its own
    read/send/commit the same way would need ``Command``'s
    frozen-dataclass identity to guard against a concurrent heartbeat
    superseding ``pending_command`` mid-send, a real, separate change,
    flagged here rather than folded in silently. It is also the category
    least implicated by the bench report: ``due_for_resend``'s own backoff
    (``RESEND_BACKOFF_S``) makes it self-limiting regardless of the
    configured budget, unlike ``poll``/``time_sync``, each due on
    essentially every tick for as long as any node stays unpolled or
    unsynced -- exactly the "``TIME_SYNC`` cannot reach the node" consequence
    the bench report names.

    Proved in ``tests/adapters/test_workers.py::
    test_a_large_channel_budget_does_not_let_a_slow_send_monopolise_the_registry_lock``
    without timing luck: a fake gateway blocks inside ``write_frame()``,
    and the test asserts ``registry_lock`` is still acquirable from another
    thread *while that write is still blocked* -- the actual property this
    fix claims, not a duration compared against a threshold.

    **Which of the three gets the budget first, and why it rotates
    instead of a fixed order:** a strict "commands always first" priority
    was considered and rejected. ``send_due_commands`` finds a due command
    on almost every tick for as long as any trap's command stays
    unconfirmed (a broken radio, forever) -- if that category always won
    the shared, scarce token, it would starve backlog polling and
    TIME_SYNC station-wide for as long as that one trap stayed
    unconfirmed, which is the same shape of bug this fix exists to close,
    just moved onto a different victim. ``rotation_index`` instead cycles
    which of the three categories is tried first each tick, so sustained
    demand from one category can slow the other two but never exclude
    them indefinitely. If operator commands (arming or disarming a trap
    holding, or about to hold, an animal) must always preempt routine
    polling for safety reasons, that is a real domain policy decision
    beyond this batch's scope -- flagged here, not picked silently.
    """
    schedule: backlog.RotationSchedule | None = None
    next_poll_due_at: datetime | None = None
    last_polled_at: dict[int, datetime] = {}
    arrivals: dict[int, float] = {} if last_frame_arrival is None else last_frame_arrival
    # CRITICAL 4 (sdd-verify field-station-p4, batch 3b): `time.monotonic()`
    # per node id, last time a *withheld* TIME_SYNC attempt was made --
    # never `clock.now()`. The withheld cadence has to keep advancing even
    # while the business clock itself is frozen or untrusted (the exact
    # `unset` case this cadence exists to bound), the same reasoning
    # `SharedBudget` above already applies to its own refill clock.
    last_withheld_attempt_at: dict[int, float] = {}
    # The clock-trust bug fix's own pacing, the withheld dict's sibling: a
    # node whose own clock_source keeps reporting untrusted is due again at
    # most once per `heartbeat_interval`, never every tick -- see this
    # function's own docstring for why a one-shot `synced_node_ids` (this
    # dict's predecessor, removed) undercounted what "synced" actually
    # meant.
    last_time_sync_attempt_at: dict[int, float] = {}
    budget = SharedBudget(
        pacing.start(
            capacity=pacing.DEFAULT_CAPACITY,
            frames_per_s=config.channel_budget_frames_per_s,
            now=time.monotonic(),
        )
    )
    rotation_index = 0

    while not stop.is_set():
        # Scheduler-throttle fix (docs/pending.md): this is the *only*
        # registry read the tick needs up front -- everything below reads
        # from this local tuple, never `registry` again, until "commands"
        # takes its own, separate, narrower lock further down.
        with registry_lock:
            node_ids = tuple(trap.node_id for trap in registry.traps())

        if node_ids:
            now = clock.now()

            if schedule is None or schedule.node_ids != node_ids:
                schedule = backlog.start(node_ids)
            if next_poll_due_at is None:
                next_poll_due_at = now

            # CRITICAL 3: rotate which of the three transmitters gets
            # first claim on the shared, scarce budget each tick -- see
            # the docstring above for why a fixed priority was
            # rejected. Plain sequential dispatch, not nested closures,
            # so none of it captures a loop variable by reference.
            order = _ROTATIONS[rotation_index % len(_ROTATIONS)]
            rotation_index += 1

            for category in order:
                if category == "poll":
                    if now < next_poll_due_at or not budget.try_send():
                        continue  # CRITICAL 3: stays due, retried next tick
                    node_id = backlog.current_target(schedule)
                    since = last_polled_at.get(node_id)
                    # Scheduler-throttle fix: `last_frame_arrival` is
                    # written by the gateway thread under this same lock
                    # (`run_gateway_loop`) -- grabbed and released
                    # immediately, never held across the write below.
                    with registry_lock:
                        last_arrival = arrivals.get(node_id, 0.0)
                    heard = since is not None and last_arrival > since.timestamp()
                    last_polled_at[node_id] = now
                    # Scheduler-throttle fix: no lock held here -- see this
                    # function's own docstring for why a blocking write
                    # must never be able to hold `registry_lock` hostage.
                    poll_node(node_id, store=store, gateway=gateway)
                    schedule = backlog.record_response(
                        schedule, heard=heard, retry_budget=config.backlog_retry_budget
                    )
                    next_poll_due_at = now + config.heartbeat_interval
                elif category == "commands":
                    # Scheduler-throttle fix: unlike "poll"/"time_sync",
                    # `send_due_commands` genuinely reads and writes
                    # `Trap.pending_command`, shared with the gateway
                    # thread's own heartbeat handling -- this stays the one
                    # category dispatched under the lock. See this
                    # function's own docstring for why splitting its own
                    # read/send/commit is flagged, not done here.
                    with registry_lock:
                        send_due_commands(
                            registry,
                            store=store,
                            gateway=gateway,
                            now=now,
                            budget=budget,
                        )
                else:  # "time_sync"
                    for node_id in node_ids:
                        # The clock-trust bug fix: due for TIME_SYNC is now
                        # the node's *own* last-reported `HEARTBEAT.
                        # clock_source` (docs/protocol/messages.md: 0
                        # unset, 1 RTC), read straight back off the store
                        # -- never a station-side "we sent one once" set
                        # (see this function's own docstring for what that
                        # cost). No lock needed: like `poll_node`/
                        # `attempt_time_sync` below, this touches the
                        # store, never `Registry`/`Trap` (confirmed by
                        # `Store.latest`'s own signature) -- the same
                        # "no lock around a call that does not need one"
                        # rule the scheduler-throttle fix already applies
                        # to every transmitter in this loop.
                        latest_heartbeat = store.latest(node_id, payloads.HEARTBEAT)
                        reported_clock_source = (
                            latest_heartbeat.fields.get("clock_source")
                            if latest_heartbeat is not None
                            else None
                        )
                        if node_clock_is_trusted(
                            reported_clock_source
                            if isinstance(reported_clock_source, int)
                            else None
                        ):
                            continue
                        # CRITICAL 3: only spend a token on an attempt
                        # that would actually transmit -- a withheld
                        # attempt (clock still unset) writes a log
                        # row, not a frame, and must not compete for
                        # airtime it never uses.
                        would_send = may_distribute_time(clock.trust())
                        if not would_send:
                            # CRITICAL 4: `attempt_time_sync` writes a
                            # `STATION_TIME_SYNC_WITHHELD` row on every
                            # call (clock-trust/spec.md's own "MUST log
                            # each withheld send") -- and, before this
                            # gate, every unsynced node was "due" on
                            # every tick, so a ten-node roster with an
                            # unset clock wrote 10 fsync'd rows/s
                            # (~189 MB/day, verify-report-2.md). The
                            # spec pins the log, not the cadence -- the
                            # defect is volume, not the log itself --
                            # so this attempts (and logs) a withheld
                            # node at most once per
                            # `config.heartbeat_interval`, the same
                            # cadence GET_BACKLOG's own per-node polling
                            # already uses above, without touching the
                            # log call or the "stays eligible" fix
                            # CRITICAL 3 already made.
                            monotonic_now = time.monotonic()
                            last_attempt = last_withheld_attempt_at.get(node_id)
                            cadence_s = config.heartbeat_interval.total_seconds()
                            if last_attempt is not None and (
                                monotonic_now - last_attempt < cadence_s
                            ):
                                continue
                            last_withheld_attempt_at[node_id] = monotonic_now
                            attempt_time_sync(node_id, clock=clock, store=store, gateway=gateway)
                            continue
                        # The clock-trust bug fix's own pacing: a real
                        # attempt is due at most once per
                        # `heartbeat_interval` too, the withheld path's
                        # own cadence just above -- the node's next
                        # HEARTBEAT is the only new evidence that could
                        # change this decision, so trying again sooner
                        # only spends airtime with nothing new to learn
                        # from it.
                        monotonic_now = time.monotonic()
                        last_attempt = last_time_sync_attempt_at.get(node_id)
                        cadence_s = config.heartbeat_interval.total_seconds()
                        if last_attempt is not None and (monotonic_now - last_attempt < cadence_s):
                            continue
                        if not budget.try_send():
                            continue  # refused this tick -- stays due, unattempted
                        # Scheduler-throttle fix: no lock held here either
                        # -- `attempt_time_sync` touches no `Registry`/
                        # `Trap` state at all (see its own signature).
                        last_time_sync_attempt_at[node_id] = monotonic_now
                        attempt_time_sync(node_id, clock=clock, store=store, gateway=gateway)
        stop.wait(tick_s)


def start_workers(
    *,
    store: Store,
    gateway: FrameGateway,
    registry: Registry,
    registry_lock: threading.Lock,
    publish: Publish,
    clock: Clock,
    config: StationConfig,
    stop: threading.Event,
    tick_s: float = 1.0,
) -> tuple[threading.Thread, WorkerStatus, threading.Thread, WorkerStatus]:
    """Build (not yet start) the gateway and scheduler threads, each
    wrapped in :func:`run_supervised` so a crash is recorded rather than
    silent (12.0c). ``app.py``'s lifespan starts them, joins them on
    shutdown, and reads the returned ``WorkerStatus`` pair for ``/health``.

    ``last_frame_arrival`` (CRITICAL 4): one plain ``dict``, shared between
    both threads exactly the way ``registry`` itself is — created once,
    here, guarded by the same ``registry_lock`` both loops already take.
    """
    gateway_status = WorkerStatus()
    scheduler_status = WorkerStatus()
    last_frame_arrival: dict[int, float] = {}

    gateway_thread = threading.Thread(
        target=run_supervised,
        args=(
            gateway_status,
            lambda: run_gateway_loop(
                gateway,
                store=store,
                registry=registry,
                registry_lock=registry_lock,
                publish=publish,
                clock=clock,
                stop=stop,
                last_frame_arrival=last_frame_arrival,
                record_payload_error=gateway.record_payload_error,
            ),
        ),
        name="feeder-station-gateway",
        daemon=True,
    )
    scheduler_thread = threading.Thread(
        target=run_supervised,
        args=(
            scheduler_status,
            lambda: run_scheduler_loop(
                store=store,
                gateway=gateway,
                registry=registry,
                registry_lock=registry_lock,
                clock=clock,
                config=config,
                stop=stop,
                last_frame_arrival=last_frame_arrival,
                tick_s=tick_s,
            ),
        ),
        name="feeder-station-scheduler",
        daemon=True,
    )
    return gateway_thread, gateway_status, scheduler_thread, scheduler_status
