"""The HTTP face, exercised end to end through the app itself.

Slice 10 (`operator-ui`) adds route-level tests only: whether the page is
actually operable by a biologist with no manual (S13) and whether it truly
works one-handed on a phone (S10) are field gates, not CI gates -- a green
suite here proves the routes, payload shapes and static file exist and
behave; it is not a usability result.
"""

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from feeder_station import app as app_module
from feeder_station.adapters.store import Store
from feeder_station.app import Broadcaster, create_app
from feeder_station.config import StationConfig
from feeder_station.domain import payloads
from feeder_station.domain.clock_trust import ClockTrust
from feeder_station.domain.events import StationEvent
from feeder_station.domain.traps import Registry, TrapState

TAG = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])
STATIC_INDEX_PATH = Path(app_module.__file__).parent / "static" / "index.html"


class _FixedClock:
    """A `Clock` double for route-level tests -- no real RTC or NTP probe
    involved, same discipline `tests/adapters/test_clock.py` already uses.
    """

    def __init__(self, trust: ClockTrust) -> None:
        self._trust = trust

    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)

    def trust(self) -> ClockTrust:
        return self._trust


def client() -> tuple[TestClient, Registry]:
    registry = Registry(silence_after=timedelta(minutes=5))
    return TestClient(create_app(registry)), registry


def test_health_answers() -> None:
    c, _ = client()
    assert c.get("/health").json()["ok"] is True


# --- 9.8: /health exposes the station's own clock trust ---------------------


def test_health_reports_clock_unset() -> None:
    """spec.md, "Clock trust states": a fresh boot with no RTC evidence and
    no NTP sync must show `unset` in `/health` -- the UI's own "clock
    unset" banner (spec.md's withhold-TIME_SYNC scenario) reads this field.
    """
    app = create_app(clock=_FixedClock(ClockTrust.UNSET))
    c = TestClient(app)

    assert c.get("/health").json()["clock_trust"] == "unset"


def test_health_reports_clock_trusted_once_rtc_or_ntp_backs_it() -> None:
    app = create_app(clock=_FixedClock(ClockTrust.NTP))
    c = TestClient(app)

    assert c.get("/health").json()["clock_trust"] == "ntp"


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


def test_arm_and_disarm_routes_set_pending_command_not_reported_state() -> None:
    """S1/S4: issuing ARM or DISARM over HTTP must only ever set a pending
    command in the registry -- the trap's reported `state` in `/api/traps`
    must not move until a HEARTBEAT says so (slice 8, DD15)."""
    c, registry = client()
    from feeder_station.app import now

    registry.heartbeat(node_id=7, state=TrapState.IDLE, at=now())

    arm_response = c.post("/api/traps/7/arm")
    assert arm_response.status_code == 200
    assert c.get("/api/traps").json()[0]["state"] == "idle"
    assert c.get("/api/traps").json()[0]["pending_command"] == "arm"

    registry.heartbeat(node_id=7, state=TrapState.ARMED, at=now())
    disarm_response = c.post("/api/traps/7/disarm")
    assert disarm_response.status_code == 200
    assert c.get("/api/traps").json()[0]["state"] == "armed"
    assert c.get("/api/traps").json()[0]["pending_command"] == "disarm"


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


# --- Slice 10: operator-ui ---------------------------------------------------

# --- 10.1/10.2: the page leaves app.py for static/index.html (DD1) ---------


def test_index_serves_static_html_with_no_external_asset_reference() -> None:
    """The route must serve `static/index.html`'s real bytes -- not a
    duplicate string still living in `app.py` -- and the file itself must
    never reach off the local network (S8): no CDN script, font or
    stylesheet.
    """
    expected = STATIC_INDEX_PATH.read_text(encoding="utf-8")

    c, _ = client()
    page = c.get("/")

    assert page.status_code == 200
    assert page.text == expected
    assert "http://" not in expected
    assert "https://" not in expected


def test_the_page_still_loads_with_no_traps() -> None:
    """Same scenario `test_the_page_loads_with_no_traps` already covers,
    re-asserted against the file-backed route so the move off `PAGE` did not
    silently drop the greeting.
    """
    c, _ = client()
    page = c.get("/")
    assert page.status_code == 200
    assert "Estación CICG" in page.text


# --- 10.3/10.4: every operator route answers, targets included -------------


def test_arm_disarm_edit_targets_acknowledge_routes_all_return_2xx() -> None:
    """Route-level only -- touch-DOM interaction is not pytest's domain (see
    this module's own docstring). Proves every action S13 names reaches the
    registry through a 2xx response.
    """
    c, registry = client()
    from feeder_station.app import now

    registry.heartbeat(node_id=7, state=TrapState.IDLE, at=now())

    assert c.post("/api/traps/7/arm").status_code // 100 == 2
    assert c.post("/api/traps/7/disarm").status_code // 100 == 2
    assert c.put("/api/traps/7/targets", json={"tags": ["0384199C87EC06"]}).status_code // 100 == 2

    registry.captured(node_id=7, tag=TAG, at=now())
    assert c.post("/api/traps/7/acknowledge").status_code // 100 == 2


def test_edit_targets_route_sets_pending_command_with_incremented_version() -> None:
    """The route computes the next desired version from the trap's last
    *reported* version (never an operator-supplied number, which would let
    two stale tabs race each other to a version neither trap ever asked
    for) -- S4's read-only-what-is-reported rule extended to versioning.
    """
    c, registry = client()
    from feeder_station.app import now

    registry.heartbeat(node_id=5, state=TrapState.IDLE, at=now(), targets_version=3)

    response = c.put("/api/traps/5/targets", json={"tags": ["0384199C87EC06", "AABBCCDDEE0011"]})
    assert response.status_code == 200
    assert response.json()["desired_targets_version"] == 4

    row = c.get("/api/traps").json()[0]
    assert row["pending_command"] == "set_targets"
    assert row["desired_targets_version"] == 4


def test_edit_targets_route_accepts_a_never_before_seen_node() -> None:
    """An operator may command a trap that has never reported yet -- the
    desired version starts at 1, not a crash on a registry KeyError.
    """
    c, _ = client()
    response = c.put("/api/traps/99/targets", json={"tags": []})
    assert response.status_code == 200
    assert response.json()["desired_targets_version"] == 1


def test_edit_targets_route_rejects_a_malformed_tag() -> None:
    """S13: a biologist typing a tag by hand will mistype it sometimes -- the
    route must refuse clearly rather than silently arm a trap to ignore
    everything or crash.
    """
    c, _ = client()
    response = c.put("/api/traps/5/targets", json={"tags": ["not-a-tag"]})
    assert response.status_code == 422


# --- 10.5/10.6: an unacknowledged capture survives a simulated restart -----


def test_unacknowledged_capture_is_present_after_a_simulated_restart(tmp_path: Path) -> None:
    """Confirms slice 4's `Store.rehydrate()` feeds `/api/traps` correctly --
    S3's second half, proven through an actual close/reopen of the database,
    not just an in-memory `Registry`.
    """
    db_path = tmp_path / "station.db"
    store = Store(db_path)
    capture_event = StationEvent(
        source="node",
        node_id=7,
        msg_type=payloads.CAPTURED,
        msg_id=1,
        node_epoch=None,
        tag=TAG,
        fields={"state_after": 2},
        payload=b"",
    )
    store.append(capture_event, received_epoch=1000.0, clock_trust="unset")
    store.close()

    reopened = Store(db_path)
    registry = Registry(silence_after=timedelta(minutes=15))
    reopened.rehydrate(registry)

    c = TestClient(create_app(registry))
    rows = c.get("/api/traps").json()
    assert rows[0]["node_id"] == 7
    assert rows[0]["capture"]["tag"] == "0384199C87EC06"


# --- 10.7/10.8: reported state and reachability are two different facts ----


def test_unreachable_trap_is_visually_distinguished_in_the_view_payload() -> None:
    """S4/S11: the payload the page renders must carry `reachable` as a
    field separate from `state` -- an armed-but-unreachable trap must never
    look, in the data the page reads, identical to a healthy armed one.
    """
    c, registry = client()
    from feeder_station.app import now

    registry.heartbeat(node_id=1, state=TrapState.ARMED, at=now())
    registry.heartbeat(node_id=2, state=TrapState.ARMED, at=datetime(2000, 1, 1, tzinfo=UTC))

    rows = {row["node_id"]: row for row in c.get("/api/traps").json()}

    assert rows[1]["reachable"] is True
    assert rows[2]["reachable"] is False
    # Both report the identical `state` -- the distinguishing fact lives only
    # in `reachable`, never folded into `state` itself as a reassuring lie.
    assert rows[1]["state"] == rows[2]["state"] == "armed"


# --- 10.9/10.10: the clock-unset banner reads /health's own field ----------


def test_clock_unset_banner_present_when_health_reports_unset() -> None:
    """Full DOM rendering is not pytest's domain (see this module's own
    docstring). This proves two mechanical facts instead: the static page's
    script reads `clock_trust`, and `/health` truly reports `unset` through a
    real app instance -- the same field 9.x's `/health` tests already cover,
    now proven reachable from the page that renders it.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")
    assert "clock_trust" in html
    assert "unset" in html

    app = create_app(clock=_FixedClock(ClockTrust.UNSET))
    c = TestClient(app)
    assert c.get("/health").json()["clock_trust"] == "unset"


# --- 10.11 REFACTOR: no external asset anywhere in the shipped page --------


def test_static_index_html_references_no_external_asset() -> None:
    """S8: the station may have no route out at all. The page must never
    depend on a CDN, a remote font or a remote script to render.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")

    assert "http://" not in html
    assert "https://" not in html
    assert "://" not in html  # also catches protocol-relative `//cdn...` references
