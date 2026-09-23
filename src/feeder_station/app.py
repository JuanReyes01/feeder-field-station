"""The station's HTTP face.

Plain HTTP on the local network, no internet required (S8). Live updates go out
over Server-Sent Events: one-way, ordinary HTTP, and the browser reconnects on
its own. The push is only an accelerator — the state is always in the page, so
somebody opening the interface hours after a capture still sees it (S3).
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, field_validator

from feeder_station.adapters.clock import Clock, StationClock
from feeder_station.adapters.export import export_station
from feeder_station.adapters.serial_gateway import SerialGateway
from feeder_station.adapters.store import Store
from feeder_station.adapters.workers import WorkerStatus, start_workers
from feeder_station.config import StationConfig
from feeder_station.domain.payloads import TAG_BYTES
from feeder_station.domain.traps import Registry

# Deliberately one self-contained page: the station must work from a phone on
# a local network with no internet, so nothing is fetched from a CDN (S8,
# S10). It left `app.py` for `static/index.html` (DD1, slice 10) — a 400-line
# HTML string was not reviewable as Python. Hatchling ships this non-Python
# file inside the package (pyproject.toml), so this still works from an
# installed wheel, not only from a source checkout.
_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")

_TAG_HEX_PATTERN = re.compile(rf"[0-9A-Fa-f]{{{TAG_BYTES * 2}}}")


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
        for tag in tags:
            if not _TAG_HEX_PATTERN.fullmatch(tag):
                raise ValueError(f"tag must be {TAG_BYTES * 2} hex characters, got {tag!r}")
        return tags


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


def create_app(
    registry: Registry | None = None,
    config: StationConfig | None = None,
    clock: Clock | None = None,
    store: Store | None = None,
    gateway: SerialGateway | None = None,
) -> FastAPI:
    resolved_config = config or StationConfig.load()
    resolved_clock: Clock = clock or StationClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # DD11: the loop only exists once the app actually starts serving —
        # capture it here, not at construction time.
        app.state.broadcaster.bind_loop(asyncio.get_running_loop())

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
            threads = [gateway_thread, scheduler_thread]
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
        return {
            "ok": workers_ok,
            "clock": now().isoformat(),
            "clock_trust": app.state.clock.trust().value,
            "workers": workers,
        }

    @app.get("/api/traps")
    def traps() -> list[dict[str, Any]]:
        with app.state.registry_lock:
            rows: list[dict[str, Any]] = app.state.registry.view(now=now())
        return rows

    @app.post("/api/traps/{node_id}/arm")
    def arm(node_id: int) -> dict[str, Any]:
        with app.state.registry_lock:
            app.state.registry.command_arm(node_id, at=now())
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {"node_id": node_id, "pending_command": "arm"}

    @app.post("/api/traps/{node_id}/disarm")
    def disarm(node_id: int) -> dict[str, Any]:
        with app.state.registry_lock:
            app.state.registry.command_disarm(node_id, at=now())
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {"node_id": node_id, "pending_command": "disarm"}

    @app.post("/api/traps/{node_id}/acknowledge")
    def acknowledge(node_id: int) -> dict[str, Any]:
        with app.state.registry_lock:
            app.state.registry.acknowledge(node_id, at=now())
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {"node_id": node_id, "acknowledged": True}

    @app.put("/api/traps/{node_id}/targets")
    def set_targets(node_id: int, body: TargetsRequest) -> dict[str, Any]:
        # The desired version is computed from the trap's own last *reported*
        # version (S4), never an operator-supplied number -- an unknown node
        # (never yet heard from) starts at 1, same as any first command.
        with app.state.registry_lock:
            try:
                current_version = app.state.registry.trap(node_id).targets_version
            except KeyError:
                current_version = None
            next_version = (current_version or 0) + 1

            app.state.registry.command_set_targets(node_id, targets_version=next_version, at=now())
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {
            "node_id": node_id,
            "pending_command": "set_targets",
            "desired_targets_version": next_version,
        }

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
