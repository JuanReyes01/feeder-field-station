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
from feeder_station.adapters.serial_gateway import DeviceConfig, LinkState, SerialGateway
from feeder_station.adapters.store import Store
from feeder_station.app import Broadcaster, create_app
from feeder_station.config import StationConfig
from feeder_station.domain import frames, payloads
from feeder_station.domain.clock_trust import ClockTrust
from feeder_station.domain.events import (
    OPERATOR_ARM,
    OPERATOR_DISARM,
    OPERATOR_SET_TARGETS,
    StationEvent,
)
from feeder_station.domain.frames import Frame
from feeder_station.domain.frames import encode as encode_frame
from feeder_station.domain.serial_framing import DELIMITER, cobs_decode, cobs_encode
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

    registry.captured(node_id=7, tag=TAG, state=TrapState.FIRED, at=now())

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

    registry.captured(node_id=7, tag=TAG, state=TrapState.FIRED, at=now())
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


# --- Slice 12: auto-start-deployment ----------------------------------------

# --- 12.0a/12.0b: nothing before this slice ever started a thread ----------


class _FakePort:
    """A minimal ``SerialPort`` double, the same shape
    ``tests/adapters/test_serial_gateway.py::_FakePort`` already uses: bytes
    in, bytes out, no OS involved.
    """

    def __init__(self, *, to_read: bytes = b"") -> None:
        self._to_read = bytearray(to_read)
        self.written: list[bytes] = []

    def read(self, size: int) -> bytes:
        chunk = bytes(self._to_read[:size])
        del self._to_read[: len(chunk)]
        return chunk

    def write(self, data: bytes) -> int:
        self.written.append(bytes(data))
        return len(data)

    def close(self) -> None:
        pass


def _decode_written_msg_types(blocks: list[bytes]) -> set[int]:
    """Every ``fake_port.write()`` call the gateway ever makes is exactly
    one COBS-encoded, delimiter-terminated frame (``SerialGateway.
    write_frame``'s own contract) -- undo that to see what was actually
    sent.
    """
    types: set[int] = set()
    for block in blocks:
        assert block[-1] == DELIMITER
        types.add(frames.decode(cobs_decode(block[:-1])).msg_type)
    return types


def test_the_app_starts_its_background_workers(tmp_path: Path) -> None:
    """The test this slice exists to make pass (tasks.md, slice 12): with
    a fake port and a fake clock, starting the real app -- through
    `create_app`'s own lifespan, not a hand-assembled worker call -- must
    ingest a frame (the gateway thread, slices 5/6), run a rotation round
    (slice 7), and resend a due command (slice 8), end to end. This is the
    test that proves all four orphaned drivers this slice exists to start
    actually run: the fourth, `attempt_time_sync` (slice 9), also fires
    every tick here since the clock is trusted, but is not separately
    asserted on -- TIME_SYNC has no user-facing effect this test can read
    back other than one more frame type on the wire.
    """
    heartbeat_payload = bytes(
        [
            0x01,
            0xA8,
            0x0A,
            0x04,
            0x03,
            0x02,
            0x01,
            0x01,
            0x09,
            0x00,
            0x05,
            0x00,
            0x08,
            0x07,
            0x06,
            0x05,
        ]
    )
    raw_frame = encode_frame(
        Frame(msg_type=payloads.HEARTBEAT, node_id=11, msg_id=1, payload=heartbeat_payload)
    )
    wire = cobs_encode(raw_frame) + bytes([DELIMITER])
    fake_port = _FakePort(to_read=wire)

    store = Store(tmp_path / "station.db")
    registry = Registry(silence_after=timedelta(minutes=15))
    registry.command_disarm(9, at=datetime(2026, 1, 1, tzinfo=UTC))
    gateway = SerialGateway(DeviceConfig(explicit_path="fake"), open_port=lambda device: fake_port)

    app = create_app(
        registry=registry, clock=_FixedClock(ClockTrust.NTP), store=store, gateway=gateway
    )

    with TestClient(app) as c:
        deadline = time.monotonic() + 5.0
        ingested = False
        written_types: set[int] = set()
        while time.monotonic() < deadline:
            rows = c.get("/api/traps").json()
            ingested = any(row["node_id"] == 11 for row in rows)
            written_types = _decode_written_msg_types(fake_port.written)
            if (
                ingested
                and payloads.GET_BACKLOG in written_types
                and payloads.DISARM in written_types
            ):
                break
            time.sleep(0.02)

    assert ingested, "the gateway thread never ingested the fixture frame"
    assert payloads.GET_BACKLOG in written_types, "the scheduler thread never ran a rotation round"
    assert payloads.DISARM in written_types, "the scheduler thread never resent the due command"


def test_health_reports_workers_disabled_when_no_gateway_configured() -> None:
    """Backward compatibility: every test above this one calls `create_app`
    with no `store`/`gateway` at all -- `/health`'s new `workers` field
    must not flip `ok` to `False` just because nothing was configured.
    """
    c, _ = client()
    body = c.get("/health").json()

    assert body["ok"] is True
    assert body["workers"]["gateway"] == {"enabled": False, "alive": False, "last_error": None}
    assert body["workers"]["scheduler"] == {"enabled": False, "alive": False, "last_error": None}


# --- 12.0c: a dead worker is visible in /health, not silently deaf ---------


class _CrashingGateway:
    def ensure_connected(self, *, stop: object) -> bool:
        raise RuntimeError("boom -- simulated dead radio")

    def read_frames(self, *, size: int = 4096) -> list[bytes]:  # pragma: no cover - unreachable
        return []

    def write_frame(self, raw_frame: bytes) -> bool:  # pragma: no cover - unreachable
        return True

    def record_payload_error(self) -> None:  # pragma: no cover - unreachable
        return None

    @property
    def link_state(self) -> LinkState:
        # This gateway never even reaches `connect_once()` -- `ensure_
        # connected` raises before it would set one up -- so the link is
        # honestly reported down, same as a real `SerialGateway` that has
        # never resolved a device.
        return LinkState(
            connected=False,
            device=None,
            since=None,
            frames_in=0,
            frames_out=0,
            decode_errors=0,
            payload_errors=0,
            last_error=None,
        )


def test_a_dead_gateway_thread_is_visible_in_health(tmp_path: Path) -> None:
    """12.0c, and the exact cost this slice's own tasks.md names for
    skipping it: without this, a thread that dies in week three leaves the
    station serving `/health` perfectly, every trap looking unreachable,
    and nothing anywhere saying the radio -- not the traps -- is at fault.
    """
    store = Store(tmp_path / "station.db")
    app = create_app(store=store, gateway=_CrashingGateway())  # type: ignore[arg-type]

    with TestClient(app) as c:
        deadline = time.monotonic() + 2.0
        body = c.get("/health").json()
        while time.monotonic() < deadline and body["workers"]["gateway"]["alive"]:
            time.sleep(0.02)
            body = c.get("/health").json()

    assert body["workers"]["gateway"]["enabled"] is True
    assert body["workers"]["gateway"]["alive"] is False
    assert body["workers"]["gateway"]["last_error"] is not None
    assert "boom" in body["workers"]["gateway"]["last_error"]
    assert body["ok"] is False


# --- CRITICAL 5 (sdd-verify, field-station-p4): /health surfaces link state


class _LinkDroppingGateway:
    """A `SerialGateway`-shaped double whose link starts connected and can
    be dropped on demand -- the gateway thread itself never needs to do
    anything real; this only exists to control `link_state` deterministically.
    """

    def __init__(self) -> None:
        self._connected = True

    def ensure_connected(self, *, stop: object) -> bool:
        return True

    def read_frames(self, *, size: int = 4096) -> list[bytes]:
        return []

    def record_payload_error(self) -> None:  # pragma: no cover - unused here
        return None

    def write_frame(self, raw_frame: bytes) -> bool:  # pragma: no cover - unused here
        return True

    @property
    def link_state(self) -> LinkState:
        return LinkState(
            connected=self._connected,
            device="fake" if self._connected else None,
            since=None,
            frames_in=0,
            frames_out=0,
            decode_errors=0,
            payload_errors=0,
            last_error=None if self._connected else "simulated link drop",
        )

    def drop(self) -> None:
        self._connected = False


def test_link_drop_surfaces_in_health(tmp_path: Path) -> None:
    """radio-gateway/spec.md, "Device resolution and reconnect": "/health
    reports link state down within the next poll interval, and the gateway
    does not pretend the link is up." Proven through a real request against
    a real, fully booted app -- the test with this exact name in
    `tests/adapters/test_serial_gateway.py` was named for `/health` and
    never once requested it; `app.state.gateway` was stored at `app.py`
    construction time and never read by anything until this fix.
    """
    store = Store(tmp_path / "station.db")
    gateway = _LinkDroppingGateway()
    app = create_app(store=store, gateway=gateway)  # type: ignore[arg-type]

    with TestClient(app) as c:
        body = c.get("/health").json()
        assert body["link"]["connected"] is True
        assert body["ok"] is True

        gateway.drop()

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and body["link"]["connected"]:
            time.sleep(0.02)
            body = c.get("/health").json()

    assert body["link"]["connected"] is False
    assert body["link"]["last_error"] == "simulated link drop"
    # "the gateway does not pretend the link is up": a dead radio must
    # flip the top-level `ok`, not only a field nobody without prior
    # knowledge would think to check.
    assert body["ok"] is False


# --- 11.10: export_station() gets its trigger, deferred out of slice 11 ----


def test_export_route_produces_a_station_export_folder(tmp_path: Path) -> None:
    """11.10, closed here: `export_station()` (slice 11) was fully built
    and tested with nothing calling it -- slice 11's own PR boundary did
    not include `app.py`/`cli.py` (see that slice's apply-progress notes),
    and design.md's own concurrency diagram lists `export` alongside
    `arm`/`acknowledge` as one HTTP threadpool handler, which is where
    this route lives.
    """
    store = Store(tmp_path / "station.db")
    app = create_app(store=store)
    dest = tmp_path / "usb"
    dest.mkdir()

    c = TestClient(app)
    response = c.post("/api/export", json={"dest_root": str(dest)})

    assert response.status_code == 200
    folder = Path(response.json()["folder"])
    assert folder.is_dir()
    assert (folder / "station.db").exists()
    assert (folder / "events.csv").exists()
    assert (folder / "README.txt").exists()


def test_export_route_without_a_store_reports_unavailable_not_a_crash() -> None:
    c, _ = client()
    response = c.post("/api/export", json={"dest_root": "/tmp/wherever"})
    assert response.status_code == 503


# --- Slice 13: operator-actions-are-real ------------------------------------

# --- 13.1/13.2: acknowledging MUST be a store write, not an in-memory flag -


def test_acknowledging_a_capture_survives_a_restart_through_the_route(tmp_path: Path) -> None:
    """event-store/spec.md, "Capture persists until acknowledged": today's
    `acknowledge()` route only ever clears `Trap.capture` in memory --
    nothing writes an `OPERATOR_CAPTURE_ACKNOWLEDGED` row, so a power cut
    right after this POST resurrects the capture on the next boot and sends
    somebody back out to a trap already seen to. Proven through the real
    HTTP route -- not a hand-called `Store.append` -- and a genuine
    close/reopen/rehydrate of the database, the same discipline
    `test_unacknowledged_capture_is_present_after_a_simulated_restart`
    already uses for the un-acknowledged half of this same requirement.
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

    registry = Registry(silence_after=timedelta(minutes=15))
    store.rehydrate(registry)
    assert registry.trap(7).capture is not None  # sanity: the fixture seeds a real capture

    c = TestClient(create_app(registry=registry, store=store))
    response = c.post("/api/traps/7/acknowledge")
    assert response.status_code == 200
    assert c.get("/api/traps").json()[0]["capture"] is None  # cleared in memory too

    store.close()

    reopened = Store(db_path)
    fresh_registry = Registry(silence_after=timedelta(minutes=15))
    reopened.rehydrate(fresh_registry)
    reopened.close()

    # WARNING D (verify-report-batch2a.md): a trap for node 7 IS created by
    # rehydration here -- the acknowledged CAPTURED row still supplies the
    # winning state fact (CRITICAL 1: a CAPTURED is a report, made by the
    # node, about itself, independent of whether it was later
    # acknowledged). The proof is narrower than "nothing left": it is that
    # acknowledging cleared the *capture object* durably, not merely the
    # in-memory flag -- `Registry.acknowledge()`'s own scope (traps.py) is
    # `trap.capture = None` alone, never `state`.
    rows = TestClient(create_app(fresh_registry)).get("/api/traps").json()
    assert all(row["capture"] is None for row in rows)


# --- 13.3/13.4: ARM/DISARM are recorded, not only applied in memory --------


def test_arm_and_disarm_are_recorded_as_operator_events(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")
    c = TestClient(create_app(store=store))

    assert c.post("/api/traps/7/arm").status_code == 200
    assert c.post("/api/traps/7/disarm").status_code == 200

    armed = store.latest(7, OPERATOR_ARM)
    disarmed = store.latest(7, OPERATOR_DISARM)
    assert armed is not None
    assert disarmed is not None
    assert armed.source == "operator"
    assert disarmed.source == "operator"


def test_operator_routes_still_work_with_no_store_configured() -> None:
    """Backward compatibility: every route-level test above this one calls
    `create_app` with no `store` at all -- an operator action must stay
    2xx and in-memory-only, the same degraded-but-honest shape `/api/export`
    already reports with a 503 rather than a crash.
    """
    c, _ = client()
    assert c.post("/api/traps/7/arm").status_code == 200
    assert c.post("/api/traps/7/disarm").status_code == 200
    assert c.post("/api/traps/7/acknowledge").status_code == 200


# --- 13.5/13.6: PUT /targets persists the exact bytes, not just the version


def test_the_submitted_tags_are_stored_not_just_the_version(tmp_path: Path) -> None:
    """trap-commands/spec.md's target-list requirement is only honest once
    the bytes an operator actually typed are durable -- before this slice,
    the route validated them, bumped `targets_version`, and threw them away.
    """
    db_path = tmp_path / "station.db"
    store = Store(db_path)
    c = TestClient(create_app(store=store))

    response = c.put("/api/traps/5/targets", json={"tags": ["0384199C87EC06", "AABBCCDDEE0011"]})
    assert response.status_code == 200
    version = response.json()["desired_targets_version"]

    store.close()
    reopened = Store(db_path)
    row = reopened.latest(5, OPERATOR_SET_TARGETS)
    reopened.close()

    assert row is not None
    assert row.fields["targets_version"] == version
    assert row.fields["tags"] == ["0384199c87ec06", "aabbccddee0011"]


# --- 13.11/13.12: the page shows what a trap is actually hunting -----------


def test_the_ui_shows_the_tags_a_trap_is_actually_hunting() -> None:
    c, _ = client()
    response = c.put("/api/traps/5/targets", json={"tags": ["0384199C87EC06", "AABBCCDDEE0011"]})
    assert response.status_code == 200

    row = c.get("/api/traps").json()[0]
    assert row["targets"] == ["0384199C87EC06", "AABBCCDDEE0011"]
