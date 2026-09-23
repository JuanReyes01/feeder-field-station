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
from functools import partial

from feeder_station.adapters.clock import Clock, attempt_time_sync
from feeder_station.adapters.command_sender import send_due_commands
from feeder_station.adapters.ingest import Publish, ingest_frame
from feeder_station.adapters.scheduler import run_rotation_round
from feeder_station.adapters.serial_gateway import SerialGateway
from feeder_station.adapters.store import Store
from feeder_station.config import StationConfig
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
                )


def _recently_reachable(
    registry: Registry, node_id: int, now: datetime, config: StationConfig
) -> bool:
    """``run_rotation_round``'s own ``responded`` contract
    (``adapters/scheduler.py``) asks whether a node "answered" *this* poll
    — but a ``GET_BACKLOG`` reply arrives asynchronously, on the gateway
    thread, not synchronously inside this call. The nearest honest fact
    this thread has instead: has the node reported *anything* recently, by
    the identical reachability window S11 already defines
    (``domain/traps.py::Trap.is_reachable``, ``config.py``'s
    ``silence_after``). This is this slice's own adapter-level choice, not
    a rule design.md pins — flagged for verify.
    """
    try:
        trap = registry.trap(node_id)
    except KeyError:
        return False
    return trap.is_reachable(now, config.silence_after)


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
) -> None:
    """design.md section 7's "scheduler thread — 1 s tick": backlog
    rotation, command retries, and TIME_SYNC — the three drivers slices 7,
    8 and 9 left fully tested with nothing calling them, on the tick this
    slice finally starts.

    ``stop.wait(tick_s)`` — not ``time.sleep`` — is what makes shutdown
    interrupt a long tick immediately rather than waiting it out.

    **TIME_SYNC policy, named because design.md pins only the *check*
    (S12/D7, "at the moment of sending"), never a resend cadence:** this
    loop attempts it once per node id, the first tick that id appears in
    the roster, tracked in ``synced_node_ids`` for the life of this thread.
    A node once trusted rarely needs correcting again inside one station
    uptime; if the clock is ``unset``, ``attempt_time_sync`` itself
    withholds the frame and records why every time it is asked, so a fuller
    resend policy for *that* refusal is not something this slice invents
    either — flagged for verify, same as the ``responded`` heuristic above.
    """
    synced_node_ids: set[int] = set()
    while not stop.is_set():
        with registry_lock:
            node_ids = tuple(trap.node_id for trap in registry.traps())
            if node_ids:
                now = clock.now()
                run_rotation_round(
                    node_ids,
                    store=store,
                    gateway=gateway,
                    # `partial` binds this tick's `now` immediately, right
                    # here -- not a closure over the loop variable `now`,
                    # which the next tick would otherwise reassign out from
                    # under a plain lambda.
                    responded=partial(_recently_reachable, registry, now=now, config=config),
                    retry_budget=config.backlog_retry_budget,
                )
                send_due_commands(
                    registry,
                    store=store,
                    gateway=gateway,
                    now=now,
                    arm_duration_s=config.arm_duration.total_seconds(),
                )
                for node_id in node_ids:
                    if node_id not in synced_node_ids:
                        attempt_time_sync(node_id, clock=clock, store=store, gateway=gateway)
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
    """
    gateway_status = WorkerStatus()
    scheduler_status = WorkerStatus()

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
                tick_s=tick_s,
            ),
        ),
        name="feeder-station-scheduler",
        daemon=True,
    )
    return gateway_thread, gateway_status, scheduler_thread, scheduler_status
