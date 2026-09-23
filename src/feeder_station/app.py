"""The station's HTTP face.

Plain HTTP on the local network, no internet required (S8). Live updates go out
over Server-Sent Events: one-way, ordinary HTTP, and the browser reconnects on
its own. The push is only an accelerator — the state is always in the page, so
somebody opening the interface hours after a capture still sees it (S3).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from feeder_station.config import StationConfig
from feeder_station.domain.traps import Registry


def now() -> datetime:
    return datetime.now(UTC)


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


def create_app(registry: Registry | None = None, config: StationConfig | None = None) -> FastAPI:
    resolved_config = config or StationConfig.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # DD11: the loop only exists once the app actually starts serving —
        # capture it here, not at construction time.
        app.state.broadcaster.bind_loop(asyncio.get_running_loop())
        yield

    app = FastAPI(title="CICG field station", lifespan=lifespan)
    app.state.registry = registry or Registry(silence_after=resolved_config.silence_after)
    app.state.broadcaster = Broadcaster()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "clock": now().isoformat()}

    @app.get("/api/traps")
    def traps() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = app.state.registry.view(now=now())
        return rows

    @app.post("/api/traps/{node_id}/arm")
    def arm(node_id: int) -> dict[str, Any]:
        app.state.registry.command_arm(node_id, at=now())
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {"node_id": node_id, "pending_command": "arm"}

    @app.post("/api/traps/{node_id}/disarm")
    def disarm(node_id: int) -> dict[str, Any]:
        app.state.registry.command_disarm(node_id, at=now())
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {"node_id": node_id, "pending_command": "disarm"}

    @app.post("/api/traps/{node_id}/acknowledge")
    def acknowledge(node_id: int) -> dict[str, Any]:
        app.state.registry.acknowledge(node_id, at=now())
        app.state.broadcaster.publish({"type": "traps_changed"})
        return {"node_id": node_id, "acknowledged": True}

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
        return PAGE

    return app


# Deliberately one self-contained page: the station must work from a phone on a
# local network with no internet, so nothing is fetched from a CDN (S8, S10).
PAGE = """<!doctype html>
<html lang="es">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Estación CICG</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 1rem; background: #111; color: #eee; }
  h1 { font-size: 1.1rem; font-weight: 600; }
  .trap { border: 1px solid #333; border-radius: 8px; padding: .75rem; margin: .5rem 0; }
  .capture { border-color: #b33; background: #2a1111; }
  .unreachable { opacity: .55; }
  .state { font-weight: 700; text-transform: uppercase; font-size: .8rem; letter-spacing: .05em; }
  button { font: inherit; padding: .5rem 1rem; border-radius: 6px; border: 1px solid #555;
           background: #222; color: #eee; }
</style>
<h1>Estación CICG</h1>
<div id="traps">Cargando…</div>
<script>
async function refresh() {
  const traps = await (await fetch('/api/traps')).json();
  document.getElementById('traps').innerHTML = traps.length ? traps.map(t => `
    <div class="trap ${t.capture ? 'capture' : ''} ${t.reachable ? '' : 'unreachable'}">
      <div class="state">Trampa ${t.node_id} · ${t.state}${t.reachable ? '' : ' · SIN CONTACTO'}</div>
      ${t.capture ? `<p>Capturó ${t.capture.tag}<br><small>${t.capture.at}</small></p>
        <button onclick="ack(${t.node_id})">Atendida</button>` : ''}
      ${t.pending_command ? `<p><small>${t.pending_command} pendiente de confirmar</small></p>` : ''}
    </div>`).join('') : '<p>Ninguna trampa se ha reportado todavía.</p>';
}
async function ack(id) {
  await fetch(`/api/traps/${id}/acknowledge`, {method: 'POST'});
  refresh();
}
new EventSource('/api/events').onmessage = refresh;
refresh();
setInterval(refresh, 30000);
</script>
</html>
"""
