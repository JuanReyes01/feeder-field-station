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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from feeder_station.adapters.clock import Clock, attempt_time_sync
from feeder_station.adapters.command_sender import send_due_commands
from feeder_station.adapters.ingest import Publish, ingest_frame
from feeder_station.adapters.scheduler import poll_node
from feeder_station.adapters.serial_gateway import SerialGateway
from feeder_station.adapters.store import Store
from feeder_station.config import StationConfig
from feeder_station.domain import backlog
from feeder_station.domain.traps import Registry


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
    gateway: SerialGateway,
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
    gateway: SerialGateway,
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
       ``run_rotation_round`` itself is untouched and still fully tested
       (``tests/adapters/test_backlog_rotation.py``) as a correct,
       reusable full-sweep primitive — this loop simply no longer calls it
       every tick, since nothing at that cadence can hold the airtime
       budget regardless of how the sweep itself is written.

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

    TIME_SYNC policy is otherwise unchanged from before this fix (named
    because design.md pins only the *check*, S12/D7, never a resend
    cadence): attempted once per node id, the first tick that id appears
    in the roster, tracked in ``synced_node_ids`` for the life of this
    thread — **except that CRITICAL 3 is fixed here too:** a *withheld*
    attempt (``attempt_time_sync`` returning ``False``, clock still
    ``unset``) no longer counts as synced. Before this fix the return
    value was discarded and every node was marked synced unconditionally
    on its first tick, so a node polled while the clock was still
    ``unset`` was never offered ``TIME_SYNC`` again even once NTP later
    made the clock trustworthy — severing exactly the boot-with-no-
    internet recovery S12 exists for.
    """
    schedule: backlog.RotationSchedule | None = None
    next_poll_due_at: datetime | None = None
    last_polled_at: dict[int, datetime] = {}
    arrivals: dict[int, float] = {} if last_frame_arrival is None else last_frame_arrival
    synced_node_ids: set[int] = set()

    while not stop.is_set():
        with registry_lock:
            node_ids = tuple(trap.node_id for trap in registry.traps())
            if node_ids:
                now = clock.now()

                if schedule is None or schedule.node_ids != node_ids:
                    schedule = backlog.start(node_ids)
                if next_poll_due_at is None:
                    next_poll_due_at = now

                if now >= next_poll_due_at:
                    node_id = backlog.current_target(schedule)
                    since = last_polled_at.get(node_id)
                    heard = since is not None and arrivals.get(node_id, 0.0) > since.timestamp()
                    last_polled_at[node_id] = now
                    poll_node(node_id, store=store, gateway=gateway)
                    schedule = backlog.record_response(
                        schedule, heard=heard, retry_budget=config.backlog_retry_budget
                    )
                    next_poll_due_at = now + config.heartbeat_interval

                send_due_commands(
                    registry,
                    store=store,
                    gateway=gateway,
                    now=now,
                    arm_duration_s=config.arm_duration.total_seconds(),
                )
                for node_id in node_ids:
                    # CRITICAL 3: only a frame actually sent counts as
                    # synced -- a withheld attempt must stay eligible for
                    # retry on a later tick, once the clock is trusted.
                    if node_id not in synced_node_ids and attempt_time_sync(
                        node_id, clock=clock, store=store, gateway=gateway
                    ):
                        synced_node_ids.add(node_id)
        stop.wait(tick_s)


def start_workers(
    *,
    store: Store,
    gateway: SerialGateway,
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
