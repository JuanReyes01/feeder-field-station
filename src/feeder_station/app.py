"""The station's HTTP face.

Plain HTTP on the local network, no internet required (S8). Live updates go out
over Server-Sent Events: one-way, ordinary HTTP, and the browser reconnects on
its own. The push is only an accelerator — the state is always in the page, so
somebody opening the interface hours after a capture still sees it (S3).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import threading
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel, field_validator

from feeder_station.adapters.clock import Clock, StationClock
from feeder_station.adapters.export import export_station
from feeder_station.adapters.serial_gateway import LinkState
from feeder_station.adapters.specimen_import import read_specimens
from feeder_station.adapters.store import SpecimenRow, Store, StoredEvent
from feeder_station.adapters.workers import FrameGateway, WorkerStatus, start_workers
from feeder_station.config import StationConfig
from feeder_station.domain import event_log, pacing, payloads
from feeder_station.domain.events import (
    OPERATOR_ARM,
    OPERATOR_CANCEL_COMMAND,
    OPERATOR_CAPTURE_ACKNOWLEDGED,
    OPERATOR_DISARM,
    OPERATOR_SET_TARGETS,
    StationEvent,
)
from feeder_station.domain.payloads import TAG_BYTES, TAGS_PER_SET_TARGETS_FRAME
from feeder_station.domain.tags import parse_tag, to_biomark
from feeder_station.domain.traps import Registry, TrapState
from feeder_station.domain.visits import DEFAULT_GAP_SECONDS, Reading, Visit, group_visits

# Deliberately one self-contained page: the station must work from a phone on
# a local network with no internet, so nothing is fetched from a CDN (S8,
# S10). It left `app.py` for `static/index.html` (DD1, slice 10) — a 400-line
# HTML string was not reviewable as Python. Hatchling ships this non-Python
# file inside the package (pyproject.toml), so this still works from an
# installed wheel, not only from a source checkout.
_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

_TAG_HEX_PATTERN = re.compile(rf"[0-9A-Fa-f]{{{TAG_BYTES * 2}}}")

# BEHAVIOUR 1 (verify-report-final.md, sdd-verify field-station-p4): the
# other half of the fix in adapters/command_sender.py -- charging N tokens
# for an N-chunk SET_TARGETS (domain/pacing.py::try_consume_n) only helps if
# a chunked send can ever actually collect N tokens. The shared bucket never
# holds more than pacing.DEFAULT_CAPACITY tokens at once, by design -- that
# is the station's *whole* burst allowance, shared across every trap in a
# roster of at least ten (S5), not a per-trap grant -- so a tag list needing
# more chunks than the bucket can ever hold would be refused by the budget
# forever, not merely paced. Bounding the list at exactly that many chunks
# is the only value that keeps every accepted edit sendable: an operator-
# typed list has no legitimate reason to need more (S13: this is meant to
# stay small enough to type or paste by hand), and it caps how much of the
# station's entire burst allowance one trap's single edit can ever claim.
_MAX_TARGET_TAGS = int(pacing.DEFAULT_CAPACITY) * TAGS_PER_SET_TARGETS_FRAME

# station/live-reads-view: how far back GET /api/reads looks. A page left
# open for hours must not have its view grow unbounded -- old reads simply
# roll off, the same "state that was true once" discipline every other
# operator-facing fact on this page already applies (docs/pending.md,
# "Operator-facing gaps"). This bounds the *query*, never a computed
# "visit" -- the operator's own "still here" grouping threshold (a client-
# side slider, design.md 3.2's amendment) is a separate, smaller number
# applied at render time, never sent here and never stored.
_READS_VIEW_WINDOW = timedelta(minutes=30)
_RECONCILE_INTERVAL_SECONDS = 5.0
_MAX_IMPORT_BYTES = 20 * 1024 * 1024
_LOG = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(UTC)


class ExportRequest(BaseModel):
    """11.10: the operator-facing half of ``export_station()`` (slice 11) —
    the destination is a path the runbook (``docs/deploy.md``) tells an
    operator to point at a mounted drive; this route does not enumerate
    mount points itself (design.md's own "the UI offers mount points"
    remains a follow-up, not this slice's scope).
    """

    dest_root: str


class TargetsRequest(BaseModel):
    """A new target-tag list from the operator (S1/S2's `SET_TARGETS`,
    slice 2's wire codec). Validated here, at the HTTP boundary, so a
    mistyped tag never reaches the registry as a silently-wrong version
    bump -- S13: a biologist typing a 14-character hex code by hand will
    mistype it sometimes, and the route must refuse clearly, not crash or
    guess.
    """

    tags: list[str]

    @field_validator("tags")
    @classmethod
    def _tags_are_well_formed(cls, tags: list[str]) -> list[str]:
        # BEHAVIOUR 1: rejected before the version bump and the store write
        # below, not merely paced -- an unbounded list is how the chunked
        # SET_TARGETS budget bypass was reachable through this route at all.
        if len(tags) > _MAX_TARGET_TAGS:
            raise ValueError(
                f"tag list has {len(tags)} tags, more than the {_MAX_TARGET_TAGS} this "
                "station can ever budget tokens for in one chunked SET_TARGETS send"
            )
        for tag in tags:
            if not _TAG_HEX_PATTERN.fullmatch(tag):
                raise ValueError(f"tag must be {TAG_BYTES * 2} hex characters, got {tag!r}")
        return tags


class InterestRequest(BaseModel):
    all_traps: bool = False
    node_ids: list[int] = []


class BulkInterestRequest(InterestRequest):
    tags: list[str]


class TrapProfileRequest(BaseModel):
    name: str | None = None
    has_latch: bool | None = None
    setup_done: bool | None = None


class NewSpecimenRequest(BaseModel):
    pit: str
    of_interest: bool = False
    species: str | None = None
    species_code: str | None = None
    sex: str | None = None
    age: str | None = None
    band_number: str | None = None


class SpecimenUpdateRequest(BaseModel):
    """station/specimen-roster (docs/pending.md, "The station has one kind
    of storage and needs two"): an edit to one roster row. Every field is
    optional -- ``PATCH /api/specimens/{tag}`` reads only the keys the
    request actually set (``.model_dump(exclude_unset=True)``, at the
    route), so omitting a field leaves it untouched, and an operator can
    submit ``{"species": "..."}`` alone without accidentally clearing
    ``band_number`` or ``of_interest``.

    ``species``/``band_number`` are the only reference fields pending.md
    names beyond the flag -- "keep the set small and obvious; do not invent
    a taxonomy."
    """

    species: str | None = None
    species_code: str | None = None
    sex: str | None = None
    age: str | None = None
    band_number: str | None = None
    of_interest: bool | None = None
    registered: bool | None = None


class Broadcaster:
    """Fans one event out to every open browser."""

    def __init__(self) -> None:
        self._clients: set[asyncio.Queue[str]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Capture the running loop, once, at lifespan startup (DD11).

        Nothing publishes before the app has started
        serving, so binding here — rather than guessing a loop eagerly in
        `__init__`, before one necessarily exists — is safe.
        """
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self._clients.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._clients.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        """Safe from any thread (DD11). This is the only way to publish.

        Python's documentation is explicit: "This class [asyncio.Queue] is
        not thread safe." So the enqueue always happens on the loop, marshalled
        there by `call_soon_threadsafe` — the one piece of `asyncio` the
        documentation names as callable from any thread. Calling it from the
        loop's own thread is fine too, which is why there is a single path and
        not a choice.

        There used to be two methods, a fast one for "the loop thread" and a
        safe one for everything else, on the belief that every route handler
        ran on the loop. That belief was wrong: Starlette runs a plain `def`
        endpoint in a threadpool, so `arm()` and `acknowledge()` were already
        the unsafe case. A correctness rule nobody can get wrong beats a rule
        everybody is told to remember.
        """
        loop = self._loop
        if loop is None:
            # No loop bound yet — the lifespan has not started (for example
            # a bare `TestClient(create_app())` used without its own `with`
            # block). Publishing is never S9-critical (design.md section 3,
            # step 9): dropping it here is the same best-effort a slow
            # subscriber's own `QueueFull` already gets.
            return
        loop.call_soon_threadsafe(self._fan_out, json.dumps(event))

    def _fan_out(self, payload: str) -> None:
        """The actual enqueue. Runs on the loop thread, always."""
        for queue in list(self._clients):
            # A browser that cannot keep up is dropped from this event, never
            # blocked on: the state stays correct in the next page load.
            with suppress(asyncio.QueueFull):
                queue.put_nowait(payload)


def _record_operator_event(
    store: Store | None,
    clock: Clock,
    *,
    node_id: int,
    msg_type: int,
    fields: Mapping[str, object] | None = None,
) -> None:
    """The durable half of every operator action (event-store spec.md,
    "Capture persists until acknowledged": acknowledgement **MUST be a
    store write, not merely an in-memory flag** -- the same discipline
    this slice applies to arm, disarm and edit-targets too, not only
    acknowledge).

    Called before the registry changes and before the SSE publish -- the
    same S9 ordering `adapters/ingest.py` holds for an inbound frame
    (decode -> write+fsync -> ACK -> registry -> publish), mirrored for the
    outbound direction: a route must never announce "done" over SSE for
    something not yet durable.

    A no-op when this station has no store configured -- the identical
    degraded-but-honest shape `/api/export` already reports with a 503
    rather than a crash. Every route calling this stays 2xx either way: a
    `--no-gateway`, store-less station (or any pre-slice-13 test's bare
    `create_app()`) simply cannot promise a durability it has nowhere to
    put, and an operator working the interface in that mode still needs
    arm/disarm/acknowledge/edit-targets to work in memory.
    """
    if store is None:
        return
    event = StationEvent(
        source="operator",
        node_id=node_id,
        msg_type=msg_type,
        msg_id=None,
        node_epoch=None,
        tag=None,
        fields=fields or {},
        payload=None,
    )
    store.append(event, received_epoch=clock.now().timestamp(), clock_trust=clock.trust().value)


def _specimen_to_json(row: SpecimenRow) -> dict[str, Any]:
    return {
        "tag": row.tag.hex().upper(),
        "biomark": to_biomark(row.tag),
        "registered": row.registered,
        "all_traps": row.of_interest,
        "trap_ids": list(row.trap_ids),
        "species_code": row.species_code,
        "sex": row.sex,
        "age": row.age,
        "species": row.species,
        "band_number": row.band_number,
        "of_interest": row.of_interest,
        "times_seen": row.times_seen,
        # station/live-reads-view precedent: an ISO timestamp, never a raw
        # epoch, is what every other operator-facing time on this page uses.
        "last_seen": (
            datetime.fromtimestamp(row.last_seen_epoch, tz=UTC).isoformat()
            if row.last_seen_epoch is not None
            else None
        ),
    }


def _push_targets(
    store: Store, registry: Registry, clock: Clock, *, node_id: int, tags: Sequence[bytes]
) -> None:
    """Ask one trap to hold this list, durably first. Caller holds ``registry_lock``."""
    trap = registry.trap(node_id)
    next_version = (trap.targets_version or 0) + 1
    _record_operator_event(
        store,
        clock,
        node_id=node_id,
        msg_type=OPERATOR_SET_TARGETS,
        fields={"targets_version": next_version, "tags": [tag.hex() for tag in tags]},
    )
    registry.command_set_targets(node_id, targets_version=next_version, at=now(), tags=tags)


def _push_changed_targets(store: Store, registry: Registry, clock: Clock) -> None:
    """After an interest edit: send each trap its own list, only if it changed.
    Caller holds ``registry_lock``."""
    for trap in registry.traps():
        wanted = store.wanted_tags_for(trap.node_id)
        if set(trap.desired_tags or ()) != set(wanted):
            _push_targets(store, registry, clock, node_id=trap.node_id, tags=wanted)


def reconcile_targets(store: Store, registry: Registry, clock: Clock, at: datetime) -> list[int]:
    """Lists are sent on their own: bring every reachable trap whose reported
    list differs from what it should hunt back into line.

    This is what handles a trap that joins for the first time, or one holding an
    old list from an earlier test, with nobody having to notice. It only acts on
    a report that is current, and never while a command is outstanding, so it
    cannot loop: each push waits for its own confirmation and a fresh report
    before this looks at that trap again. Caller holds ``registry_lock``.
    """
    pushed: list[int] = []
    for trap in registry.traps():
        if not trap.is_reachable(at) or trap.pending_command is not None:
            continue
        if trap.reported_tags is None or trap.pending_targets_fetch is not None:
            continue
        if trap.reported_targets_version != trap.targets_version:
            continue
        wanted = store.wanted_tags_for(trap.node_id)
        if set(wanted) != set(trap.reported_tags):
            _push_targets(store, registry, clock, node_id=trap.node_id, tags=wanted)
            pushed.append(trap.node_id)
    return pushed


def trap_code(node_id: int) -> str:
    """How a trap is named: its id in hex. A trap takes its id from its own
    MAC (firmware components/node_identity), so this is also what is written
    on the box."""
    return f"{node_id:04X}"


def _trap_names(node_id: int, place: str | None) -> dict[str, Any]:
    code = trap_code(node_id)
    return {
        "code": code,
        "place": place,
        "name": f"Trampa {code}" + (f" · {place}" if place else ""),
    }


def finish_test_fires(
    store: Store | None, registry: Registry, clock: Clock, at: datetime
) -> list[int]:
    """Second half of a test fire: once the trap reports it is no longer armed,
    send ARM. Caller holds ``registry_lock``."""
    armed: list[int] = []
    for trap in registry.traps():
        if not trap.rearm_after_test_fire or trap.pending_command is not None:
            continue
        if not trap.is_reachable(at) or trap.state is TrapState.ARMED:
            continue
        trap.rearm_after_test_fire = False
        _record_operator_event(store, clock, node_id=trap.node_id, msg_type=OPERATOR_ARM)
        registry.command_arm(trap.node_id, at=at)
        armed.append(trap.node_id)
    return armed


def _reading_of(event: StoredEvent) -> Reading | None:
    if event.tag is None:
        return None
    return Reading(
        node_id=event.node_id,
        tag=event.tag,
        epoch=float(event.node_epoch) if event.node_epoch is not None else event.received_epoch,
        clock_trusted=event.node_epoch is not None,
    )


def _readings(events: Iterable[StoredEvent]) -> list[Reading]:
    return [reading for reading in map(_reading_of, events) if reading is not None]


def _subject_fields(row: SpecimenRow | None) -> dict[str, Any]:
    """How a tag reads to a person. A tag nobody registered is not a subject:
    every marked bird is on the list, so it is most likely a misread."""
    known = row is not None and row.registered
    return {
        "known": known,
        "species": row.species if row is not None and known else None,
        "label": (
            (row.species or "Sin especie") if row is not None and known else "Lectura desconocida"
        ),
    }


def _local_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).astimezone().isoformat(timespec="seconds")


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds")


def _start_of_today() -> float:
    return now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _csv_response(
    filename: str, header: Sequence[str], rows: Iterable[Sequence[object]]
) -> Response:
    """UTF-8 with a BOM, so Excel shows the accents right on a double click."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _run_reconcile_loop(
    store: Store,
    registry: Registry,
    lock: threading.Lock,
    clock: Clock,
    stop: threading.Event,
    publish: Any,
) -> None:
    while not stop.wait(_RECONCILE_INTERVAL_SECONDS):
        try:
            with lock:
                pushed = reconcile_targets(store, registry, clock, now())
                pushed += finish_test_fires(store, registry, clock, now())
            if pushed:
                publish({"type": "traps_changed"})
        except Exception:  # noqa: BLE001 - one bad pass must not end the loop for good
            _LOG.exception("could not reconcile target lists")


def _worker_health(thread: threading.Thread | None, status: WorkerStatus | None) -> dict[str, Any]:
    """One entry of ``/health``'s ``workers`` field (12.0c). ``enabled`` is
    ``False`` exactly when no ``store``/``gateway`` was ever passed to
    ``create_app`` — the pre-slice-12 shape every earlier test still uses —
    in which case there is no thread to be dead, so it never counts
    against ``ok``.
    """
    if thread is None or status is None:
        return {"enabled": False, "alive": False, "last_error": None}
    return {"enabled": True, "alive": thread.is_alive(), "last_error": status.last_error}


class _HealthReportingGateway(FrameGateway, Protocol):
    """``/health`` needs one thing neither ``adapters/workers.py`` call
    site does: a snapshot of the link itself. A narrow Protocol of its
    own, extending ``FrameGateway`` rather than widening it -- the same
    "one Protocol per consumer" discipline ``adapters/ingest.py``'s
    ``AckPort`` and this module's own ``FrameGateway`` already keep, so
    ``run_gateway_loop``'s own type does not have to know this route
    exists.
    """

    @property
    def link_state(self) -> LinkState: ...


def create_app(
    registry: Registry | None = None,
    config: StationConfig | None = None,
    clock: Clock | None = None,
    store: Store | None = None,
    gateway: _HealthReportingGateway | None = None,
) -> FastAPI:
    resolved_config = config or StationConfig.load()
    resolved_clock: Clock = clock or StationClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # DD11: the loop only exists once the app actually starts serving —
        # capture it here, not at construction time.
        app.state.broadcaster.bind_loop(asyncio.get_running_loop())

        # CRITICAL 1 (sdd-verify, field-station-p4): design.md section 6,
        # "at boot, before the HTTP server accepts" -- before this, nothing
        # in `src/` ever called `store.rehydrate()` at all; `cli.build_app`
        # (the real ExecStart path) built an empty `Registry` and left it
        # empty for the life of the process. Must run before the worker
        # threads start below: `run_scheduler_loop` derives its node roster
        # from this same registry, so an unrehydrated registry silences the
        # scheduler too, not only `/api/traps`.
        if store is not None:
            store.rehydrate(app.state.registry)

        # 12.0a/12.0b: nothing before this slice ever started a thread. The
        # gateway is off by default (design.md section 7) so every test
        # that never passes `store`/`gateway` keeps running this lifespan
        # with no threads and no hardware, exactly as before.
        threads: list[threading.Thread] = []
        if store is not None and gateway is not None:
            gateway_thread, gateway_status, scheduler_thread, scheduler_status = start_workers(
                store=store,
                gateway=gateway,
                registry=app.state.registry,
                registry_lock=app.state.registry_lock,
                publish=app.state.broadcaster.publish,
                clock=resolved_clock,
                config=resolved_config,
                stop=app.state.stop_event,
            )
            app.state.gateway_thread = gateway_thread
            app.state.gateway_status = gateway_status
            app.state.scheduler_thread = scheduler_thread
            app.state.scheduler_status = scheduler_status
            reconcile_thread = threading.Thread(
                target=_run_reconcile_loop,
                args=(
                    store,
                    app.state.registry,
                    app.state.registry_lock,
                    resolved_clock,
                    app.state.stop_event,
                    app.state.broadcaster.publish,
                ),
                name="feeder-station-reconcile",
                daemon=True,
            )
            threads = [gateway_thread, scheduler_thread, reconcile_thread]
            for thread in threads:
                thread.start()

        try:
            yield
        finally:
            # design.md section 7's "Shutdown" row: stop flags, join both
            # threads with a timeout, `PRAGMA optimize`, close -- in that
            # order, so `store.close()` never races a thread still reading
            # or writing through its own connection.
            app.state.stop_event.set()
            for thread in threads:
                thread.join(timeout=5.0)
            if store is not None:
                store.optimize()
                store.close()

    app = FastAPI(title="CICG field station", lifespan=lifespan)
    app.state.registry = registry or Registry(silence_after=resolved_config.silence_after)
    app.state.broadcaster = Broadcaster()
    app.state.clock = resolved_clock
    app.state.config = resolved_config
    app.state.store = store
    app.state.gateway = gateway
    # design.md section 7's concurrency table, "Registry" row: "One
    # threading.Lock, owned by the shell... because domain/ stays free of
    # concurrency primitives." Before this slice, exactly one thread (the
    # HTTP threadpool) ever touched the registry; this slice gives it two
    # more callers (the gateway and scheduler threads), so this lock stops
    # being theoretical.
    app.state.registry_lock = threading.Lock()
    app.state.stop_event = threading.Event()
    app.state.gateway_thread = None
    app.state.gateway_status = None
    app.state.scheduler_thread = None
    app.state.scheduler_status = None

    @app.get("/health")
    def health() -> dict[str, Any]:
        # S12/D7 (spec.md, "Clock trust states"): the operator UI's "clock
        # unset" banner and every scheduled TIME_SYNC read this same value —
        # never a separate, potentially-stale cache of it.
        workers = {
            "gateway": _worker_health(app.state.gateway_thread, app.state.gateway_status),
            "scheduler": _worker_health(app.state.scheduler_thread, app.state.scheduler_status),
        }
        # 12.0c: a worker that was started but is no longer alive must flip
        # `ok` -- the whole point is that this must be visible without an
        # operator having to know to look at a separate field. A worker
        # never enabled (no gateway configured) never counts against it.
        workers_ok = all(not w["enabled"] or w["alive"] for w in workers.values())

        # CRITICAL 5 (sdd-verify, field-station-p4): `SerialGateway.
        # link_state` had no reader anywhere in `src/` -- `/health` said
        # `ok: true` with the radio unplugged, the exact inversion of
        # radio-gateway/spec.md's "the gateway does not pretend the link is
        # up." `None` (never a fabricated `connected: True`) when this
        # station has no gateway configured at all (`--no-gateway`, or any
        # pre-slice-12 test's bare `create_app()`).
        gateway = app.state.gateway
        link: dict[str, Any] | None = None
        link_ok = True
        if gateway is not None:
            state = gateway.link_state
            link = {
                "transport": "serial",
                "connected": state.connected,
                "device": state.device,
                "since": state.since,
                "frames_in": state.frames_in,
                "frames_out": state.frames_out,
                "decode_errors": state.decode_errors,
                # WARNING C (sdd-verify field-station-p4 batch 1): a
                # payload-level decode failure, distinguishable from the
                # COBS-level `decode_errors` above.
                "payload_errors": state.payload_errors,
                "last_error": state.last_error,
            }
            # "fold connected into ok" (verify-report.md's own fix
            # wording): a station with no gateway configured never
            # counts against `ok` -- the same "never enabled, never
            # counts" discipline `workers_ok` already applies above.
            link_ok = bool(state.connected)

        # CRITICAL 2 (sdd-verify, field-station-p4): "surface it, don't
        # hide it" — the same convention `link`'s `decode_errors` already
        # sets. A row `store.rehydrate()` skipped at boot (adapters/
        # store.py::rehydrate) is real information for whoever is looking
        # at this station, even though it never crashes the boot any more.
        # `None` (never a fabricated `0`) when this station has no store at
        # all, the same "never enabled, never counts" discipline `link`
        # already applies for a station with no gateway configured.
        store = app.state.store
        rehydration_errors = None if store is None else store.rehydration_errors

        # WARNING K(b) (sdd-verify field-station-p4 batch 2,
        # verify-report-batch1-round2.md): `rehydration_errors` alone read
        # byte-identical for "one unreadable row, twenty otherwise-healthy
        # nodes" and "the roster query itself died, registry empty" -- the
        # second case used to crash the boot loudly; Batch 1's containment
        # made it silent. A registry that never got a chance to exist is a
        # degraded boot, not an error count: its own field, and folded into
        # `ok` -- the same "fold connected into ok" precedent `link_ok`
        # already sets, so neither a monitoring check nor a person glancing
        # at `ok` alone can read this as a healthy, merely quiet station.
        roster_lost = None if store is None else store.roster_lost

        # WARNING 4 (sdd-verify field-station-p4 batch 2 close-out,
        # verify-report-batch2.md): `roster_lost` only ever catches a
        # roster *query* that raises -- a query that succeeds while
        # returning fewer nodes than the store actually holds (a partial
        # write, a `node_cursor` row lost while its node's other rows
        # survive) read identically to a fully healthy boot. Compare the
        # two counts `Store.rehydrate` already keeps: a roster that comes
        # back short says so, the same "fold connected into ok" precedent
        # `link_ok`/`roster_lost` already set.
        roster_nodes_expected = None if store is None else store.roster_nodes_expected
        roster_nodes_rehydrated = None if store is None else store.roster_nodes_rehydrated
        roster_incomplete = (
            roster_nodes_expected is not None
            and roster_nodes_rehydrated is not None
            and roster_nodes_rehydrated < roster_nodes_expected
        )
        roster_ok = roster_lost is not True and not roster_incomplete

        return {
            "ok": workers_ok and link_ok and roster_ok,
            "clock": now().isoformat(),
            "clock_trust": app.state.clock.trust().value,
            "workers": workers,
            "link": link,
            "rehydration_errors": rehydration_errors,
            "roster_lost": roster_lost,
            "roster_nodes_expected": roster_nodes_expected,
            "roster_nodes_rehydrated": roster_nodes_rehydrated,
        }

    @app.get("/api/traps")
    def traps() -> list[dict[str, Any]]:
        with app.state.registry_lock:
            rows: list[dict[str, Any]] = app.state.registry.view(now=now())
        return rows

    @app.get("/api/reads")
    def reads() -> dict[str, list[dict[str, Any]]]:
        """station/live-reads-view: the raw, ungrouped rows the operator's
        live view groups by tag client-side, at render time (design.md
        3.2's amendment -- the "still here" gap is a viewer control, never
        computed or cached here). Partitioned by ``node_id`` here, once,
        so a tag seen at two feeders is never merged into one feeder's
        list downstream.

        An empty object, never a 503, when this station has no store
        configured -- the same "never enabled, never counts" honesty
        `/health`'s own `link`/`rehydration_errors` already apply: no
        durable log to read from is a degraded-but-normal shape, not an
        error.
        """
        store = app.state.store
        if store is None:
            return {}
        cutoff = now().timestamp() - _READS_VIEW_WINDOW.total_seconds()
        by_node: dict[str, list[dict[str, Any]]] = {}
        for event in store.recent_reads(since_epoch=cutoff):
            if event.tag is None:
                continue
            by_node.setdefault(str(event.node_id), []).append(
                {
                    "tag": event.tag.hex().upper(),
                    "node_epoch": event.node_epoch,
                    "received_epoch": event.received_epoch,
                }
            )
        return by_node

    # station/remote-arm-restored: `029b80c` removed this route ("remove
    # the remote arm affordance") because arming meant a person walking to
    # the trap and placing the cord by hand -- no downlink could do that,
    # settled by the hardware, not a preference. That comment's own
    # premise has since changed: the bench was rewired so one ESP32 is the
    # whole trap, and the firmware runs the arming sweep itself on a
    # remote `ARM` ("ahora todo se maneja desde la esp"). The other half of
    # `029b80c`'s reasoning is untouched and is exactly why this route is
    # safe to bring back now and not before: `034720b` made a `FAULT`
    # naming a refused command cancel it (`Registry.fault`, `domain/
    # commands.py::FAULT_CODE_REFUSES`), so an older node that still
    # refuses `ARM` with `ARM_REQUIRES_PRESENCE` gets this pending command
    # cancelled after one refusal, not retried forever burning the
    # airtime budget `TIME_SYNC` needs (the exact failure `034720b`'s own
    # commit message measured: nine `FAULT` rows for one `POST /arm`).
    #
    # Same shape as `disarm` below: only ever sets a pending command (S4)
    # -- never marks the trap `armed` from this route, since nothing here
    # has heard the trap say so yet. Durable before the registry changes
    # and the SSE publish, the same S9 ordering every operator route in
    # this module already holds (`_record_operator_event`'s own
    # docstring).
    #
    # The honest caveat this does not fix: firmware's `wall_reached()` is
    # still a placeholder (`docs/pending.md`, "Nothing knows whether the
    # arm actually reached the wall") -- a HEARTBEAT reporting `armed` only
    # means the commanded sweep finished, not that the cord is on the
    # mechanism. That is a hardware gap this route cannot close; what it
    # does not do is overstate it -- see `static/index.html`'s own arm
    # caveat, rendered from `armed_via_remote_command_at` below.
    @app.post("/api/traps/{node_id}/arm")
    def arm(node_id: int) -> dict[str, Any]:
        with app.state.registry_lock:
            _record_operator_event(
                app.state.store, app.state.clock, node_id=node_id, msg_type=OPERATOR_ARM
            )
            app.state.registry.command_arm(node_id, at=now())
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {"node_id": node_id, "pending_command": "arm"}

    @app.post("/api/traps/{node_id}/disarm")
    def disarm(node_id: int) -> dict[str, Any]:
        with app.state.registry_lock:
            _record_operator_event(
                app.state.store, app.state.clock, node_id=node_id, msg_type=OPERATOR_DISARM
            )
            app.state.registry.command_disarm(node_id, at=now())
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {"node_id": node_id, "pending_command": "disarm"}

    @app.post("/api/traps/{node_id}/test-fire")
    def test_fire(node_id: int) -> dict[str, Any]:
        """Release and re-arm, to check the latch: DISARM now, ARM once the
        trap reports it is released (``finish_test_fires``). No capture is
        recorded."""
        with app.state.registry_lock:
            try:
                trap = app.state.registry.trap(node_id)
            except KeyError:
                raise HTTPException(status_code=404, detail="unknown trap") from None
            _record_operator_event(
                app.state.store,
                app.state.clock,
                node_id=node_id,
                msg_type=OPERATOR_DISARM,
                fields={"test_fire": True},
            )
            app.state.registry.command_disarm(node_id, at=now())
            trap.rearm_after_test_fire = True
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {"node_id": node_id, "pending_command": "disarm"}

    @app.post("/api/traps/{node_id}/acknowledge")
    def acknowledge(node_id: int, result: str | None = None) -> dict[str, Any]:
        # event-store spec.md, "Capture persists until acknowledged":
        # acknowledgement MUST be a store write, not merely an in-memory
        # flag -- written before the registry changes (13.1/13.2).
        with app.state.registry_lock:
            _record_operator_event(
                app.state.store,
                app.state.clock,
                node_id=node_id,
                msg_type=OPERATOR_CAPTURE_ACKNOWLEDGED,
                fields={"result": result} if result in ("capturado", "sin_colibri") else None,
            )
            app.state.registry.acknowledge(node_id, at=now())
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {"node_id": node_id, "acknowledged": True}

    @app.post("/api/traps/{node_id}/cancel")
    def cancel_command(node_id: int) -> dict[str, Any]:
        """docs/pending.md, "There is no way to cancel a pending command":
        even with (3)'s fault-driven cancellation, a command the node
        never answers at all -- not confirmed, not refused -- has no way
        out short of an operator doing it directly. Unconditional, unlike
        `Registry.fault()`'s named-refusal check: any pending command may
        be cancelled, for any reason, the same way `acknowledge()` above
        does not special-case an unknown node either.
        """
        with app.state.registry_lock:
            _record_operator_event(
                app.state.store, app.state.clock, node_id=node_id, msg_type=OPERATOR_CANCEL_COMMAND
            )
            app.state.registry.cancel_command(node_id, at=now())
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {"node_id": node_id, "pending_command": None}

    @app.put("/api/traps/{node_id}/targets")
    def set_targets(node_id: int, body: TargetsRequest) -> dict[str, Any]:
        # The desired version is computed from the trap's own last *reported*
        # version (S4), never an operator-supplied number -- an unknown node
        # (never yet heard from) starts at 1, same as any first command.
        # `TargetsRequest`'s own validator already guarantees every tag is
        # exactly `TAG_BYTES * 2` hex characters, so `bytes.fromhex` here
        # cannot raise.
        tags = [bytes.fromhex(tag) for tag in body.tags]
        with app.state.registry_lock:
            try:
                current_version = app.state.registry.trap(node_id).targets_version
            except KeyError:
                current_version = None
            next_version = (current_version or 0) + 1

            # trap-commands spec.md: nothing anywhere stored what a trap is
            # hunting before this slice -- `encode_set_targets` had no
            # caller. The exact bytes, not only the version, are now a
            # store row `adapters/command_sender.py` reads back to build a
            # real frame (13.5-13.8).
            _record_operator_event(
                app.state.store,
                app.state.clock,
                node_id=node_id,
                msg_type=OPERATOR_SET_TARGETS,
                fields={
                    "targets_version": next_version,
                    "tags": [tag.hex() for tag in tags],
                },
            )
            app.state.registry.command_set_targets(
                node_id, targets_version=next_version, at=now(), tags=tags
            )
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {
            "node_id": node_id,
            "pending_command": "set_targets",
            "desired_targets_version": next_version,
        }

    @app.get("/api/specimens")
    def specimens() -> dict[str, Any]:
        """station/specimen-roster: what the operator's roster page lists.

        An empty roster, never a 503, when this station has no store
        configured -- the same "never enabled, never counts" honesty
        ``/api/reads``'s own no-store shape already applies.
        """
        store = app.state.store
        rows: list[SpecimenRow] = [] if store is None else store.specimens()
        return {
            "specimens": [_specimen_to_json(row) for row in rows],
            "of_interest_count": sum(1 for row in rows if row.of_interest),
            "of_interest_limit": _MAX_TARGET_TAGS,
        }

    @app.patch("/api/specimens/{tag_hex}")
    def update_specimen(tag_hex: str, body: SpecimenUpdateRequest) -> dict[str, Any]:
        """station/specimen-roster: mark a specimen wanted (or not), or
        record its species/band number. Only an edit that actually touches
        ``of_interest`` recomputes and re-pushes the shared target list
        (:func:`_push_changed_targets`) -- a species/band-number
        edit alone must never bump any trap's ``targets_version``.

        A specimen is never created here -- pending.md: "the roster grows
        from the data", read the other way round: nothing may be
        pre-registered through this door either (404 for an unknown tag).
        """
        if app.state.store is None:
            raise HTTPException(status_code=503, detail="no store configured for this station")
        if not _TAG_HEX_PATTERN.fullmatch(tag_hex):
            raise HTTPException(
                status_code=422,
                detail=f"tag must be {TAG_BYTES * 2} hex characters, got {tag_hex!r}",
            )
        tag = bytes.fromhex(tag_hex)
        updates = body.model_dump(exclude_unset=True)

        with app.state.registry_lock:
            rows = app.state.store.specimens()
            existing = next((row for row in rows if row.tag == tag), None)
            if existing is None:
                raise HTTPException(status_code=404, detail="unknown specimen")

            # pending.md: "dissolves the 81-tag-per-trap ceiling into a
            # selection over a roster" -- dissolves, not removes. One
            # shared SET_TARGETS still cannot outgrow what the station can
            # ever budget tokens for in one chunked send (`_MAX_TARGET_TAGS`,
            # the same bound `TargetsRequest` already enforces per edit).
            # Checked only when this edit would *add* a new tag to the
            # wanted set -- turning one off, or editing species/band number,
            # can never make the list longer.
            if updates.get("of_interest") is True and not existing.of_interest:
                already_wanted = sum(1 for row in rows if row.of_interest)
                if already_wanted >= _MAX_TARGET_TAGS:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"{already_wanted} specimens are already of interest, the most "
                            f"{_MAX_TARGET_TAGS} this station can ever budget tokens for in one "
                            "chunked SET_TARGETS send -- unmark one before adding another"
                        ),
                    )

            app.state.store.update_specimen(tag, updates)

            if "of_interest" in updates:
                _push_changed_targets(app.state.store, app.state.registry, app.state.clock)

            updated_row = next(row for row in app.state.store.specimens() if row.tag == tag)

        app.state.broadcaster.publish({"type": "traps_changed"})
        return _specimen_to_json(updated_row)

    def _require_store() -> Store:
        store = app.state.store
        if store is None:
            raise HTTPException(status_code=503, detail="no store configured for this station")
        result: Store = store
        return result

    def _tag_from_path(tag_hex: str) -> bytes:
        if not _TAG_HEX_PATTERN.fullmatch(tag_hex):
            raise HTTPException(status_code=422, detail="tag must be 14 hex characters")
        return bytes.fromhex(tag_hex)

    def _check_list_limit(store: Store, changes: Mapping[bytes, InterestRequest]) -> None:
        """No trap may end up hunting more than one SET_TARGETS can carry."""
        rows = store.specimens()
        known_traps = {trap.node_id for trap in app.state.registry.traps()}
        for node_id in known_traps | {n for c in changes.values() for n in c.node_ids}:
            count = 0
            for row in rows:
                change = changes.get(row.tag)
                if change is not None:
                    count += change.all_traps or node_id in change.node_ids
                else:
                    count += row.of_interest or node_id in row.trap_ids
            if count > _MAX_TARGET_TAGS:
                raise HTTPException(
                    status_code=422,
                    detail=f"Una trampa admite hasta {_MAX_TARGET_TAGS} sujetos de interés.",
                )

    @app.get("/api/overview")
    def overview() -> dict[str, Any]:
        """Everything the home page shows about every trap, in one read."""
        with app.state.registry_lock:
            traps: list[dict[str, Any]] = app.state.registry.view(now=now())
        store = app.state.store
        if store is None:
            for trap in traps:
                trap.update(**_trap_names(trap["node_id"], None), has_latch=None, setup_done=False)
            return {"traps": traps, "visits_today": 0, "subjects_today": 0, "captures_today": 0}

        today = _start_of_today()
        visits = group_visits(_readings(store.reads_between(since_epoch=today)))
        visits_by_node: dict[int, int] = {}
        for visit in visits:
            visits_by_node[visit.node_id] = visits_by_node.get(visit.node_id, 0) + 1
        profiles = store.trap_profiles()
        specimens = {row.tag: row for row in store.specimens()}

        for trap in traps:
            node_id = trap["node_id"]
            profile = profiles.get(node_id)
            heartbeat = store.latest(node_id, payloads.HEARTBEAT)
            clock_epoch = heartbeat.fields.get("clock_epoch") if heartbeat else None
            wanted = store.wanted_tags_for(node_id)
            trap.update(
                **_trap_names(node_id, profile.name if profile else None),
                has_latch=profile.has_latch if profile else None,
                setup_done=profile.setup_done if profile else False,
                clock_epoch=clock_epoch,
                clock_source=heartbeat.fields.get("clock_source") if heartbeat else None,
                clock_offset_s=(
                    clock_epoch - heartbeat.received_epoch
                    if heartbeat and isinstance(clock_epoch, int) and clock_epoch > 0
                    else None
                ),
                visits_today=visits_by_node.get(node_id, 0),
                wanted=[
                    {
                        "tag": tag.hex().upper(),
                        "biomark": to_biomark(tag),
                        **_subject_fields(specimens.get(tag)),
                    }
                    for tag in wanted
                ],
                reported=(
                    None
                    if trap["reported_targets"] is None
                    else [
                        {
                            "tag": tag,
                            "biomark": to_biomark(bytes.fromhex(tag)),
                            **_subject_fields(specimens.get(bytes.fromhex(tag))),
                        }
                        for tag in trap["reported_targets"]
                    ]
                ),
            )
        return {
            "traps": traps,
            "visits_today": len(visits),
            "subjects_today": len(
                {v.tag for v in visits if (r := specimens.get(v.tag)) and r.registered}
            ),
            "captures_today": sum(1 for c in store.captures() if c.epoch >= today),
        }

    @app.patch("/api/traps/{node_id}/profile")
    def update_trap_profile(node_id: int, body: TrapProfileRequest) -> dict[str, Any]:
        updates = body.model_dump(exclude_unset=True)
        _require_store().update_trap_profile(node_id, updates)
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {"node_id": node_id, **updates}

    @app.post("/api/specimens")
    def add_specimen(body: NewSpecimenRequest) -> dict[str, Any]:
        store = _require_store()
        tag = parse_tag(body.pit)
        if tag is None:
            raise HTTPException(status_code=422, detail="El PIT no es válido.")
        fields = {
            key: value
            for key, value in body.model_dump(exclude={"pit", "of_interest"}).items()
            if value is not None
        }
        with app.state.registry_lock:
            if body.of_interest:
                _check_list_limit(store, {tag: InterestRequest(all_traps=True)})
            store.register_specimens({tag: fields})
            if body.of_interest:
                store.set_interest(tag, all_traps=True, node_ids=[])
                _push_changed_targets(store, app.state.registry, app.state.clock)
        app.state.broadcaster.publish({"type": "traps_changed"})
        return _specimen_to_json(next(r for r in store.specimens() if r.tag == tag))

    @app.put("/api/specimens/{tag_hex}/interest")
    def set_interest(tag_hex: str, body: InterestRequest) -> dict[str, Any]:
        store = _require_store()
        tag = _tag_from_path(tag_hex)
        with app.state.registry_lock:
            _check_list_limit(store, {tag: body})
            if not store.set_interest(tag, all_traps=body.all_traps, node_ids=body.node_ids):
                raise HTTPException(status_code=404, detail="unknown specimen")
            _push_changed_targets(store, app.state.registry, app.state.clock)
        app.state.broadcaster.publish({"type": "traps_changed"})
        return _specimen_to_json(next(r for r in store.specimens() if r.tag == tag))

    @app.post("/api/specimens/interest")
    def set_interest_for_many(body: BulkInterestRequest) -> dict[str, Any]:
        store = _require_store()
        tags = [_tag_from_path(tag) for tag in body.tags]
        with app.state.registry_lock:
            _check_list_limit(store, {tag: body for tag in tags})
            for tag in tags:
                store.set_interest(tag, all_traps=body.all_traps, node_ids=body.node_ids)
            _push_changed_targets(store, app.state.registry, app.state.clock)
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {"updated": len(tags)}

    @app.post("/api/specimens/import")
    async def import_specimens(request: Request, filename: str = "lista.xlsx") -> dict[str, Any]:
        """The file arrives as the raw request body, so the station needs no
        multipart dependency for this one upload."""
        store = _require_store()
        data = await request.body()
        if len(data) > _MAX_IMPORT_BYTES:
            raise HTTPException(status_code=413, detail="El archivo es demasiado grande.")
        table = await asyncio.to_thread(read_specimens, filename, data)
        created, updated = await asyncio.to_thread(store.register_specimens, table.specimens)
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {
            "created": created,
            "updated": updated,
            "errors": table.errors[:50],
            "error_count": len(table.errors),
            "warnings": table.warnings[:50],
            "warning_count": len(table.warnings),
        }

    def _visit_to_json(
        visit: Visit, specimens: Mapping[bytes, SpecimenRow], at: float
    ) -> dict[str, Any]:
        return {
            "node_id": visit.node_id,
            "tag": visit.tag.hex().upper(),
            "biomark": to_biomark(visit.tag),
            **_subject_fields(specimens.get(visit.tag)),
            "start_epoch": visit.start,
            "duration_s": visit.duration,
            "reads": visit.reads,
            "clock_trusted": visit.clock_trusted,
            "open": at - visit.end <= DEFAULT_GAP_SECONDS,
        }

    @app.get("/api/visits")
    def visits(
        node_id: int | None = None, tag: str | None = None, days: int = 1, limit: int = 300
    ) -> dict[str, Any]:
        store = app.state.store
        if store is None:
            return {"visits": [], "gap_seconds": DEFAULT_GAP_SECONDS}
        since = _start_of_today() - (max(days, 1) - 1) * 86400
        readings = _readings(store.reads_between(since_epoch=since))
        specimens = {row.tag: row for row in store.specimens()}
        grouped = group_visits(readings)
        if node_id is not None:
            grouped = [v for v in grouped if v.node_id == node_id]
        if tag is not None:
            wanted_tag = _tag_from_path(tag)
            grouped = [v for v in grouped if v.tag == wanted_tag]
        at = now().timestamp()
        return {
            "visits": [_visit_to_json(v, specimens, at) for v in grouped[:limit]],
            "gap_seconds": DEFAULT_GAP_SECONDS,
        }

    @app.get("/api/captures")
    def captures() -> dict[str, Any]:
        store = app.state.store
        if store is None:
            return {"captures": []}
        specimens = {row.tag: row for row in store.specimens()}
        return {
            "captures": [
                {
                    "node_id": row.node_id,
                    "biomark": to_biomark(row.tag) if row.tag else None,
                    **_subject_fields(specimens.get(row.tag) if row.tag else None),
                    "epoch": row.epoch,
                    "result": row.result,
                }
                for row in store.captures()
            ]
        }

    @app.get("/api/log")
    def log(kind: str = "all", node_id: int | None = None, limit: int = 100) -> dict[str, Any]:
        store = app.state.store
        if store is None:
            return {"events": []}
        if kind == "all":
            msg_types = event_log.LOGGED_MSG_TYPES
        elif kind in event_log.KIND_MSG_TYPES:
            msg_types = event_log.KIND_MSG_TYPES[kind]
        else:
            raise HTTPException(status_code=422, detail=f"unknown kind {kind!r}")
        specimens = {row.tag: row for row in store.specimens()}
        rows: list[dict[str, Any]] = []
        for event in store.log_events(msg_types, limit=min(limit, 500), node_id=node_id):
            who = ""
            if event.tag is not None:
                subject = _subject_fields(specimens.get(event.tag))
                name = subject["label"] if subject["known"] else "tag desconocido"
                who = f"{name} ({to_biomark(event.tag)})"
            entry = event_log.describe(event.msg_type, event.fields, who)
            if entry is not None:
                rows.append(
                    {
                        "seq": event.seq,
                        "node_id": event.node_id,
                        "kind": entry.kind,
                        "text": entry.text,
                        "epoch": event.received_epoch,
                    }
                )
        return {"events": rows}

    @app.get("/api/export/reads.csv")
    def export_reads() -> Response:
        store = _require_store()
        specimens = {row.tag: row for row in store.specimens()}
        places = {node_id: p.name or "" for node_id, p in store.trap_profiles().items()}

        def rows() -> Iterable[Sequence[object]]:
            for event in store.reads_between():
                if event.tag is None:
                    continue
                row = specimens.get(event.tag)
                known = row is not None and row.registered
                epoch = event.node_epoch if event.node_epoch is not None else event.received_epoch
                yield (
                    _local_iso(epoch),
                    _utc_iso(epoch),
                    "si" if event.node_epoch is not None else "no",
                    trap_code(event.node_id),
                    places.get(event.node_id, ""),
                    to_biomark(event.tag),
                    event.tag.hex().upper(),
                    "registrado" if known else "lectura desconocida",
                    (row.species or "") if row and known else "",
                    (row.species_code or "") if row and known else "",
                    (row.sex or "") if row and known else "",
                    (row.age or "") if row and known else "",
                    (row.band_number or "") if row and known else "",
                )

        stamp = now().astimezone().strftime("%Y%m%d-%H%M")
        return _csv_response(
            f"lecturas-cicg-{stamp}.csv",
            (
                "fecha_hora_local",
                "fecha_hora_utc",
                "hora_confiable",
                "trampa",
                "lugar",
                "biomark",
                "tag_hex",
                "clasificacion",
                "especie",
                "codigo_especie",
                "sexo",
                "edad",
                "anillo",
            ),
            rows(),
        )

    @app.get("/api/export/visits.csv")
    def export_visits() -> Response:
        store = _require_store()
        specimens = {row.tag: row for row in store.specimens()}
        places = {node_id: p.name or "" for node_id, p in store.trap_profiles().items()}

        def rows() -> Iterable[Sequence[object]]:
            for visit in group_visits(_readings(store.reads_between())):
                row = specimens.get(visit.tag)
                known = row is not None and row.registered
                yield (
                    _local_iso(visit.start),
                    round(visit.duration, 1),
                    visit.reads,
                    "si" if visit.clock_trusted else "no",
                    trap_code(visit.node_id),
                    places.get(visit.node_id, ""),
                    to_biomark(visit.tag),
                    "registrado" if known else "lectura desconocida",
                    (row.species or "") if row and known else "",
                )

        stamp = now().astimezone().strftime("%Y%m%d-%H%M")
        return _csv_response(
            f"visitas-cicg-{stamp}.csv",
            (
                "inicio_local",
                "duracion_s",
                "lecturas",
                "hora_confiable",
                "trampa",
                "lugar",
                "biomark",
                "clasificacion",
                "especie",
            ),
            rows(),
        )

    @app.post("/api/export")
    def export(body: ExportRequest) -> dict[str, Any]:
        # 11.10: `export_station()` (slice 11) was fully built and tested
        # with nothing calling it. `store` is `None` exactly when
        # `create_app` was never given one (every pre-slice-12 test, and
        # any deployment started with `--no-gateway` but no `--db-path`
        # either) -- a clear 503 beats a crash or a silent no-op.
        if app.state.store is None:
            raise HTTPException(status_code=503, detail="no store configured for this station")
        folder = export_station(app.state.store, body.dest_root, now=now())
        return {"folder": str(folder)}

    @app.get("/api/events")
    async def events() -> StreamingResponse:
        queue = app.state.broadcaster.subscribe()

        async def stream() -> AsyncIterator[bytes]:
            try:
                yield b": connected\n\n"
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=20)
                    except TimeoutError:
                        yield b": keepalive\n\n"  # keeps proxies and phones from hanging up
                        continue
                    yield f"data: {payload}\n\n".encode()
            finally:
                app.state.broadcaster.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX_HTML

    return app
