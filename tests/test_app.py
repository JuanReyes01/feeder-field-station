"""The HTTP face, exercised end to end through the app itself."""

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from feeder_station.app import Broadcaster, create_app
from feeder_station.config import StationConfig
from feeder_station.domain.traps import Registry, TrapState

TAG = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])


def client() -> tuple[TestClient, Registry]:
    registry = Registry(silence_after=timedelta(minutes=5))
    return TestClient(create_app(registry)), registry


def test_health_answers() -> None:
    c, _ = client()
    assert c.get("/health").json()["ok"] is True


def test_the_page_loads_with_no_traps() -> None:
    c, _ = client()
    page = c.get("/")
    assert page.status_code == 200
    assert "Estación CICG" in page.text


def test_a_capture_is_visible_to_a_browser_that_arrives_later() -> None:
    """S3: the notification is a state, so being late does not miss it."""
    c, registry = client()
    from feeder_station.app import now

    registry.captured(node_id=7, tag=TAG, at=now())

    rows = c.get("/api/traps").json()
    assert rows[0]["capture"]["tag"] == "0384199C87EC06"

    c.post("/api/traps/7/acknowledge")
    assert c.get("/api/traps").json()[0]["capture"] is None


# --- 6.1/6.2: DD11 — asyncio.Queue is documented as not thread-safe --------


def test_publish_is_safe_from_a_background_thread() -> None:
    """DD11, explicit, not a footnote.

    Python's own docs: "This class [asyncio.Queue] is not thread safe." So the
    enqueue — `Broadcaster._fan_out` — may only ever run on the loop's own
    thread, no matter which thread called `publish`. This hammers `publish`
    from a real background thread and proves every resulting enqueue landed on
    the loop.

    It is the test that fails if `publish` is ever changed to enqueue directly
    instead of marshalling through `loop.call_soon_threadsafe`:
    `observed_thread_ids` would then hold the hammering thread's id.

    Note what is deliberately NOT asserted: that `publish` itself runs on the
    loop. It must be callable from anywhere — a route handler in Starlette's
    threadpool, the gateway thread — which is the whole point of having one
    method instead of two.
    """
    broadcaster = Broadcaster()
    queue = broadcaster.subscribe()

    loop = asyncio.new_event_loop()
    loop_errors: list[BaseException] = []
    loop.set_exception_handler(
        lambda _loop, context: loop_errors.append(
            context.get("exception") or RuntimeError(context["message"])
        )
    )
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    broadcaster.bind_loop(loop)

    observed_thread_ids: list[int] = []
    real_fan_out = broadcaster._fan_out

    def spying_fan_out(payload: str) -> None:
        observed_thread_ids.append(threading.get_ident())
        real_fan_out(payload)

    broadcaster._fan_out = spying_fan_out  # type: ignore[method-assign]

    event_count = 300

    def hammer() -> None:
        for i in range(event_count):
            broadcaster.publish({"type": "n", "i": i})

    hammer_thread = threading.Thread(target=hammer)
    hammer_thread.start()
    hammer_thread.join(timeout=5)
    assert not hammer_thread.is_alive()

    deadline = time.monotonic() + 2
    while len(observed_thread_ids) < event_count and time.monotonic() < deadline:
        time.sleep(0.005)

    loop.call_soon_threadsafe(loop.stop)
    loop_thread.join(timeout=5)
    loop.close()

    assert not loop_errors
    assert len(observed_thread_ids) == event_count
    assert hammer_thread.ident not in observed_thread_ids
    assert set(observed_thread_ids) == {loop_thread.ident}
    assert not queue.empty()


# --- 6.9/6.10: config.silence_after drives reachability, not a hardcode ----


def test_silent_node_is_marked_unreachable_within_15_minutes() -> None:
    """S11's own user decision (config.py, slice 4): unreachable after 15
    minutes (three missed 5-minute beats), never `app.py`'s old hardcoded
    `SILENCE_AFTER = timedelta(minutes=5)`. At the old hardcode's 5-minute
    threshold this fails: 6 minutes stale would already have read
    unreachable.
    """
    config = StationConfig.load(argv=[], env={})
    app = create_app(config=config)
    registry: Registry = app.state.registry
    heard_at = datetime(2026, 1, 1, tzinfo=UTC)

    registry.heartbeat(node_id=1, state=TrapState.ARMED, at=heard_at)

    six_minutes_later = heard_at + timedelta(minutes=6)
    assert registry.view(now=six_minutes_later)[0]["reachable"] is True

    sixteen_minutes_later = heard_at + timedelta(minutes=16)
    assert registry.view(now=sixteen_minutes_later)[0]["reachable"] is False
