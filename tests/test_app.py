"""The HTTP face, exercised end to end through the app itself.

Slice 10 (`operator-ui`) adds route-level tests only: whether the page is
actually operable by a biologist with no manual (S13) and whether it truly
works one-handed on a phone (S10) are field gates, not CI gates -- a green
suite here proves the routes, payload shapes and static file exist and
behave; it is not a usability result.

**Explicit decision (Batch 2e, verify-report-batch2d.md PROOF 1, recorded
in `specs/operator-ui/spec.md`'s own verification note):** the badge tests
below pin `index.html`'s source text -- a regex anchored on the ternary
that emits `.stale-badge` -- not the page's rendered DOM. That regex
cannot see content, placement or on-screen visibility, only that a
matching substring exists somewhere in the file; three mutations invented
against it (an emptied badge body, the ternary hoisted into dead code,
`.stale-badge { display: none; }`) all shipped green. This project has no
Node/browser test toolchain to build a DOM harness with (no `package.json`,
nothing in `pyproject.toml`'s `dev` group), so a fourth round of
regex-tightening was rejected in favour of recording the gap: `renderTrap`'s
actual rendered output is unverified at this layer, honestly, rather than
implied covered by a green suite.
"""

import asyncio
import re
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from feeder_station import app as app_module
from feeder_station.adapters.network_gateway import NetworkGateway, NetworkGatewayConfig
from feeder_station.adapters.serial_gateway import DeviceConfig, LinkState, SerialGateway
from feeder_station.adapters.store import Store
from feeder_station.app import Broadcaster, create_app
from feeder_station.config import StationConfig
from feeder_station.domain import frames, payloads
from feeder_station.domain.clock_trust import ClockTrust
from feeder_station.domain.events import (
    OPERATOR_ARM,
    OPERATOR_CANCEL_COMMAND,
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


def test_disarm_route_sets_pending_command_not_reported_state() -> None:
    """S1/S4: issuing DISARM over HTTP must only ever set a pending command
    in the registry -- the trap's reported `state` in `/api/traps` must
    not move until a HEARTBEAT says so (slice 8, DD15). See
    `test_arm_route_sets_pending_command_not_reported_state` below for the
    same discipline proven for ARM.
    """
    c, registry = client()
    from feeder_station.app import now

    registry.heartbeat(node_id=7, state=TrapState.ARMED, at=now())
    disarm_response = c.post("/api/traps/7/disarm")
    assert disarm_response.status_code == 200
    assert c.get("/api/traps").json()[0]["state"] == "armed"
    assert c.get("/api/traps").json()[0]["pending_command"] == "disarm"


def test_arm_route_sets_pending_command_not_reported_state() -> None:
    """S1/S4: issuing ARM over HTTP must only ever set a pending command in
    the registry -- the trap's reported `state` in `/api/traps` must not
    move until a HEARTBEAT says so (slice 8, DD15), the same discipline
    `test_disarm_route_sets_pending_command_not_reported_state` above
    already proves for DISARM.

    Supersedes `test_arm_route_no_longer_exists` (removed in `029b80c`,
    "remove the remote arm affordance"): that test was correct while the
    node had no way to arm itself and answered every `ARM` with a `FAULT`
    (`ARM_REQUIRES_PRESENCE`). The bench has since been rewired so one
    ESP32 is the whole trap and the firmware runs the arming sweep itself
    on a remote `ARM` -- the capability this test proves is real again,
    not a soft revert of the removal: an operator can never read this
    route's 200 as "the trap is armed", only as "armed" pending the
    trap's own next report, exactly like every other command on this
    page.
    """
    c, registry = client()
    from feeder_station.app import now

    registry.heartbeat(node_id=7, state=TrapState.IDLE, at=now())
    arm_response = c.post("/api/traps/7/arm")
    assert arm_response.status_code == 200
    assert c.get("/api/traps").json()[0]["state"] == "idle"
    assert c.get("/api/traps").json()[0]["pending_command"] == "arm"


def test_arm_route_surfaces_when_the_operator_asked_remotely_even_once_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/pending.md, "Nothing knows whether the arm actually reached the
    wall": firmware's `wall_reached()` is a placeholder that reports the
    commanded sweep finishing, not the cord meeting the mechanism, so a
    `HEARTBEAT` saying `armed` after this route is confirmation the sweep
    ran, never confirmation the trap is primed. `/api/traps` must keep
    saying so even after the command confirms -- the exact moment
    (`state == "armed"`) an operator most needs to know nothing verified
    the hardware, not only while the command is still `pending_command`.
    """
    arm_at = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(app_module, "now", lambda: arm_at)

    c, registry = client()
    assert c.get("/api/traps").json() == []

    assert c.post("/api/traps/7/arm").status_code == 200
    assert c.get("/api/traps").json()[0]["armed_via_remote_command_at"] == arm_at.isoformat()

    registry.heartbeat(node_id=7, state=TrapState.ARMED, at=arm_at + timedelta(seconds=30))
    row = c.get("/api/traps").json()[0]
    assert row["pending_command"] is None  # confirmed, S4
    assert row["armed_via_remote_command_at"] == arm_at.isoformat()  # caveat survives confirmation


def test_cancel_command_route_clears_a_pending_command() -> None:
    """docs/pending.md, "There is no way to cancel a pending command":
    even with fault-driven cancellation, a command the node never answers
    at all sits pending forever with no way out from the interface --
    this is that way out.
    """
    c, registry = client()
    from feeder_station.app import now

    registry.command_disarm(7, at=now())
    assert c.get("/api/traps").json()[0]["pending_command"] == "disarm"

    response = c.post("/api/traps/7/cancel")
    assert response.status_code == 200
    assert c.get("/api/traps").json()[0]["pending_command"] is None


def test_a_fault_refusing_arm_cancels_the_pending_command_the_arm_route_created() -> None:
    """The arm route and `034720b`'s fault-driven cancellation
    (`Registry.fault`, `domain/commands.py::FAULT_CODE_REFUSES`) are two
    separate pieces of code -- `tests/domain/test_traps.py` already proves
    `Registry.fault` alone, against a `Command` built directly. This
    proves they are wired to the *same* pending command end to end: the
    route's own `command_arm` call must produce a `Command` shaped the way
    `FAULT_CODE_REFUSES` expects to find one. Guards the regression
    `034720b`'s own commit message measured on the bench -- nine `FAULT`
    rows retrying one `POST /arm` forever, starving `TIME_SYNC` for hours
    -- now that the route exists again for the retry loop to run against.
    """
    c, registry = client()
    from feeder_station.app import now

    assert c.post("/api/traps/7/arm").status_code == 200
    assert c.get("/api/traps").json()[0]["pending_command"] == "arm"

    registry.fault(7, at=now(), code=5)  # STATION_FAULT_ARM_REQUIRES_PRESENCE

    assert c.get("/api/traps").json()[0]["pending_command"] is None


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


def test_arm_disarm_edit_targets_acknowledge_cancel_routes_all_return_2xx() -> None:
    """Route-level only -- touch-DOM interaction is not pytest's domain (see
    this module's own docstring). Proves every action S13 names reaches the
    registry through a 2xx response. ARM is included again now that
    `POST /api/traps/{node_id}/arm` exists (see
    `test_arm_route_sets_pending_command_not_reported_state`).
    """
    c, registry = client()
    from feeder_station.app import now

    registry.heartbeat(node_id=7, state=TrapState.IDLE, at=now())

    assert c.post("/api/traps/7/arm").status_code // 100 == 2
    assert c.post("/api/traps/7/disarm").status_code // 100 == 2
    assert c.put("/api/traps/7/targets", json={"tags": ["0384199C87EC06"]}).status_code // 100 == 2
    assert c.post("/api/traps/7/cancel").status_code // 100 == 2

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


def test_edit_targets_route_rejects_a_tag_list_the_budget_could_never_send() -> None:
    """BEHAVIOUR 1 (verify-report-final.md, sdd-verify field-station-p4): an
    unbounded list is how the chunked SET_TARGETS budget bypass was
    reachable through this route at all, and the fix on the other end
    (adapters/command_sender.py charging N tokens for an N-chunk send)
    only helps a list that can ever collect N tokens -- the shared bucket
    never holds more than `pacing.DEFAULT_CAPACITY` at once, so a list
    needing more chunks than that would be refused by the budget forever,
    not merely paced. The route must refuse it up front instead.
    """
    c, _ = client()
    one_over = app_module._MAX_TARGET_TAGS + 1
    tags = ["0384199C87EC06"] * one_over
    response = c.put("/api/traps/5/targets", json={"tags": tags})
    assert response.status_code == 422

    at_bound = app_module._MAX_TARGET_TAGS
    response = c.put("/api/traps/6/targets", json={"tags": ["0384199C87EC06"] * at_bound})
    assert response.status_code == 200


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


# --- station/live-reads-view: GET /api/reads --------------------------------
#
# The raw, per-read rows the operator's live view groups by tag client-side
# (design.md 3.2's amendment: grouping by a "still here" gap is a viewer
# control, never computed or stored here). Partitioned by node_id -- the
# view's own scope is one feeder at a time, never merged across feeders.

READ_TAG_B = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF, 0x00])


def _reads_event(*, node_id: int, msg_id: int, tag: bytes, node_epoch: int | None) -> StationEvent:
    return StationEvent(
        source="node",
        node_id=node_id,
        msg_type=payloads.READS,
        msg_id=msg_id,
        node_epoch=node_epoch,
        tag=tag,
        fields={},
        payload=b"",
    )


def test_reads_route_reports_no_reads_with_no_store_configured() -> None:
    """The same "never enabled, never counts" honesty `/health`'s
    `link`/`rehydration_errors` already apply: a store-less station (any
    pre-slice-12 test, or `--no-gateway` with no `--db-path`) has nowhere
    to read a log from, so this reports an empty view rather than crashing.
    """
    c, _ = client()
    assert c.get("/api/reads").json() == {}


def test_reads_route_keeps_the_same_tag_at_two_feeders_in_two_separate_groups(
    tmp_path: Path,
) -> None:
    """Kill: partitioning by `tag` alone (or flattening every node's reads
    into one list before the client ever sees a `node_id`) would let one
    marked animal visiting two feeders read as a single feeder's business —
    the exact "collapses two feeders into one group" defect this view must
    not ship. Each node_id must own its own, separate list of that tag's
    reads.
    """
    store = Store(tmp_path / "station.db")
    recent = time.time()
    store.append_batch(
        [_reads_event(node_id=1, msg_id=1, tag=TAG, node_epoch=int(recent))],
        received_epoch=recent,
        clock_trust="rtc",
    )
    store.append_batch(
        [_reads_event(node_id=2, msg_id=1, tag=TAG, node_epoch=int(recent))],
        received_epoch=recent,
        clock_trust="rtc",
    )

    c = TestClient(create_app(store=store))
    body = c.get("/api/reads").json()

    assert set(body.keys()) == {"1", "2"}
    assert len(body["1"]) == 1
    assert len(body["2"]) == 1
    assert body["1"][0]["tag"] == body["2"][0]["tag"] == TAG.hex().upper()


def test_reads_route_masks_an_untrusted_node_epoch_as_none_not_a_fabricated_time(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path / "station.db")
    recent = time.time()
    store.append_batch(
        [_reads_event(node_id=3, msg_id=1, tag=TAG, node_epoch=None)],
        received_epoch=recent,
        clock_trust="unset",
    )

    c = TestClient(create_app(store=store))
    row = c.get("/api/reads").json()["3"][0]

    assert row["node_epoch"] is None
    assert row["received_epoch"] == recent


def test_reads_route_excludes_reads_older_than_the_view_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view's own window (app.py's `_READS_VIEW_WINDOW`) bounds how far
    back a page left open for hours still queries -- a read from long
    before it must not resurface just because the page never reloaded.
    """
    store = Store(tmp_path / "station.db")
    old_epoch = 1_000.0
    store.append_batch(
        [_reads_event(node_id=1, msg_id=1, tag=TAG, node_epoch=int(old_epoch))],
        received_epoch=old_epoch,
        clock_trust="rtc",
    )
    recent_epoch = old_epoch + app_module._READS_VIEW_WINDOW.total_seconds() + 60.0
    store.append_batch(
        [_reads_event(node_id=1, msg_id=2, tag=READ_TAG_B, node_epoch=int(recent_epoch))],
        received_epoch=recent_epoch,
        clock_trust="rtc",
    )
    monkeypatch.setattr(app_module, "now", lambda: datetime.fromtimestamp(recent_epoch, tz=UTC))

    c = TestClient(create_app(store=store))
    rows = c.get("/api/reads").json()["1"]

    assert [r["tag"] for r in rows] == [READ_TAG_B.hex().upper()]


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


# --- WARNING 2 (verify-report-batch2.md): the stale-state badge must be
# conditional on the field, not a constant either way. -----------------------


def test_stale_state_badge_in_render_trap_depends_on_the_flag_not_a_constant() -> None:
    """Full DOM rendering is not pytest's domain (see this module's own
    docstring) -- this proves the mechanical fact a plain substring check
    cannot: `renderTrap` must *branch* on `t.state_may_be_stale`, not
    merely mention the field somewhere. A bare `"state_may_be_stale" in
    html` assertion would still pass if the badge were emitted
    unconditionally (`True` or `False`) or removed from the markup
    entirely while the field name stayed in a comment -- this pins the
    actual ternary that keys the badge's presence on the flag.

    **Extended (WARNING 2, verify-report-batch2c.md): the substring pin
    above only proved the ternary's *condition* mentions the flag -- it
    constrained neither branch's content. Two mutations shipped green:
    inverting the condition (`!t.state_may_be_stale ? <badge> : ""`, the
    signal exactly backwards) and emptying both branches (`? "" : ""`, the
    badge deleted). Anchoring the whole ternary -- condition, the truthy
    branch's actual markup, and the falsy branch's empty string -- fails
    both: an inverted condition no longer reads `${t.state_may_be_stale ?`
    literally, and empty branches no longer contain the badge markup after
    the `?`.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")

    assert re.search(r"t\.state_may_be_stale\s*\?", html), (
        "renderTrap must branch on t.state_may_be_stale, not render a constant"
    )
    pattern = re.compile(
        r"""\$\{t\.state_may_be_stale \? '<p class="stale-badge">[^<]*</p>' : ""\}"""
    )
    assert pattern.search(html), (
        "the ternary must be t.state_may_be_stale (not inverted) rendering the "
        "stale-badge markup on true and nothing on false -- not an inverted "
        "condition or two empty branches"
    )


# --- station/remote-arm-restored: the bench now runs the arming sweep on the
# node itself, so the Armar button is back (see static/index.html's own
# comment for why, superseding "Remove the remote arm affordance" below). ---


def test_the_armar_button_is_present_again() -> None:
    """Supersedes `test_the_armar_button_is_removed_from_the_static_page`
    (`029b80c`, "remove the remote arm affordance"): that test was correct
    while arming needed a person at the trap placing the cord by hand, so
    no downlink could ever do it. The bench has since been rewired so one
    ESP32 is the whole trap and the firmware runs the arming sweep itself
    on a remote `ARM` -- the button calls a route that exists again
    (`test_arm_route_sets_pending_command_not_reported_state`,
    tests/test_app.py), so hiding it would now be the dishonest state:
    an operator action available through the API but invisible on the
    page they actually use.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")
    assert 'onclick="arm(' in html
    assert ">Armar<" in html


def test_the_desarmar_button_is_still_present() -> None:
    """Standing a trap down remotely stays possible -- the release drops
    the servo without recording a capture -- so Desarmar was never at
    risk of the same removal Armar briefly went through.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")
    assert 'onclick="disarm(' in html
    assert ">Desarmar<" in html


def test_the_cancel_button_is_rendered_only_when_a_command_is_pending() -> None:
    """docs/pending.md, "There is no way to cancel a pending command": the
    same ternary-pinning discipline `test_stale_state_badge_in_render_
    trap_depends_on_the_flag_not_a_constant` above already uses for a
    conditional badge, applied to a conditional button -- a substring
    check alone would still pass if this button rendered unconditionally.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")
    pattern = re.compile(
        r"""\$\{t\.pending_command \? `<button onclick="cancelCommand\(\$\{t\.node_id\}\)">"""
        r"""[^<]*</button>` : ""\}"""
    )
    assert pattern.search(html), (
        "the Cancelar button must be conditional on t.pending_command, not "
        "rendered unconditionally or removed"
    )


def test_the_arm_caveat_is_rendered_only_for_an_armed_trap_with_a_remote_command() -> None:
    """station/remote-arm-restored: firmware's `wall_reached()` is still a
    placeholder (docs/pending.md, "Nothing knows whether the arm actually
    reached the wall"), so `state == "armed"` only ever confirms the
    commanded sweep ran, never that the cord reached the mechanism. The
    page must say so, gated on both facts at once -- a trap reporting
    `armed` for a reason this station never commanded (a legacy row, a
    heartbeat from before this capability existed) has nothing here to
    caveat, and a still-pending arm already reads as pending elsewhere on
    the page (`describePending`) -- the same ternary-pinning discipline
    `test_stale_state_badge_in_render_trap_depends_on_the_flag_not_a_
    constant` above uses, so a substring check alone could not tell an
    unconditional caveat from a correctly gated one.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")
    pattern = re.compile(
        r"""\$\{t\.state === "armed" && t\.armed_via_remote_command_at \? """
        r"""'<p class="arm-caveat">[^<]*</p>' : ""\}"""
    )
    assert pattern.search(html), (
        "the arm caveat must be conditional on both t.state === 'armed' and "
        "t.armed_via_remote_command_at, not rendered unconditionally, "
        "inverted, or removed"
    )


# --- 10.11 REFACTOR: no external asset anywhere in the shipped page --------


def test_static_index_html_references_no_external_asset() -> None:
    """S8: the station may have no route out at all. The page must never
    depend on a CDN, a remote font or a remote script to render.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")

    assert "http://" not in html
    assert "https://" not in html
    assert "://" not in html  # also catches protocol-relative `//cdn...` references


# --- station/live-reads-view: the page's own grouping/formatting logic ------
#
# Full DOM rendering is not pytest's domain (this module's own docstring):
# no Node/browser toolchain exists in this project (no package.json, nothing
# in pyproject.toml's dev group -- a deliberate, previously-recorded choice,
# not an oversight). These tests pin index.html's source text the same way
# `test_stale_state_badge_in_render_trap_depends_on_the_flag_not_a_constant`
# already does: the exact ternary/expression, not merely a substring
# mentioning the right field name somewhere.
#
# `GET /api/reads`'s own behaviour above (the real, executable proof that
# two feeders sharing a tag stay in two separate groups) is not repeated
# here -- these tests cover what only exists in the browser: the "still
# here" grouping-by-gap itself, and Spanish-language rendering.


def test_the_page_translates_trap_state_to_spanish() -> None:
    """docs/pending.md, "Operator-facing gaps": `armed`/`fired`/`idle` in
    English on a Spanish page must become Spanish, like every other label.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")

    assert re.search(r'idle:\s*"en reposo"', html)
    assert re.search(r'armed:\s*"armada"', html)
    assert re.search(r'fired:\s*"disparada"', html)
    assert re.search(r'fault:\s*"en falla"', html)
    # The ternary that assigns `state` must call the translator, not render
    # `t.state` (the raw English wire value) directly -- the same
    # condition-and-both-branches anchoring the stale-badge test already
    # uses, so an inverted condition or a bypassed translator both fail.
    assert re.search(
        r'const state = t\.state != null \? translateState\(t\.state\) : "desconocido";', html
    )


def test_ultimo_contacto_renders_a_relative_time_not_an_iso_timestamp() -> None:
    """docs/pending.md, "Operator-facing gaps": `Últ. contacto:
    2026-09-23T14:02:11-05:00` must become `hace 3 minutos` -- a live
    view's whole job is "is this happening now", which an ISO string does
    not answer at a glance.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")

    # `lastSeen` must be computed through formatRelative(), not a bare
    # escapeHtml(t.last_seen) rendering the ISO string untouched.
    assert re.search(
        r"const lastSeen = t\.last_seen \? formatRelative\(epochOf\(t\.last_seen\), nowEpoch\) "
        r': "nunca";',
        html,
    )
    assert "escapeHtml(t.last_seen)" not in html
    # formatRelative itself must actually produce "hace ..." text, not just
    # be named correctly while returning something else.
    assert re.search(r'formatRelative[^{]*\{[^}]*"hace', html, re.DOTALL)


def test_reads_are_scoped_to_one_feeder_before_being_grouped_by_tag() -> None:
    """Kill: passing the whole `/api/reads` response (every feeder's reads)
    into the tag-grouping function would let a tag seen at two feeders
    read as one feeder's business -- the exact "collapses two feeders into
    one group" defect. `renderTrap` must look up its own `node_id`'s slice
    of `readsByNode` before any grouping happens.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")

    assert re.search(r"readsByNode\[t\.node_id\]\s*\|\|\s*\[\]", html)
    # groupReadsByTag must be called with that per-node slice (`reads`),
    # not the raw `readsByNode` object itself.
    assert re.search(r"groupReadsByTag\(reads,\s*thresholdSeconds,\s*nowEpoch\)", html)


def test_a_groups_still_growing_flag_is_recomputed_from_now_not_a_constant() -> None:
    """Kill: this is the mechanical proof behind "renders a stale group as
    if it were current" -- `stillGrowing` must be an expression comparing
    `nowEpoch` (a parameter, fresh on every call) against the group's own
    `lastEpoch`, not `true`, not a value copied from the read itself.
    Anchoring the full assignment (not only the field name) catches a
    hardcoded `true` and an inverted comparison alike, the same
    "condition and both branches" discipline the stale-badge test already
    established for a ternary.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")

    assert re.search(r"g\.stillGrowing = nowEpoch - g\.lastEpoch <= thresholdSeconds;", html)
    # groupReadsByTag itself must never call Date.now() -- "now" only ever
    # arrives as this function's own parameter, so every caller (a fetch-
    # triggered refresh, or the no-fetch tick below) controls it.
    match = re.search(r"function groupReadsByTag\([^)]*\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert match, "groupReadsByTag must be defined as a top-level function"
    assert "Date.now()" not in match.group(1)


def test_a_periodic_tick_recomputes_the_view_without_a_new_fetch() -> None:
    """A group that stops updating must stop reading "sigue aquí" as real
    time passes, even with no new data arriving (docs/pending.md's own
    defect: "state that was true once, presented as true now"). A second,
    faster `setInterval` -- distinct from the 30-second data refresh --
    that calls `rerender()` (which reads `Date.now()` itself, not a cached
    value) is what keeps that true between fetches.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")

    assert re.search(r"setInterval\(refresh,\s*30000\)", html)
    assert re.search(r"setInterval\(rerender,\s*\d+\)", html)
    match = re.search(r"function rerender\(\)\s*\{(.*?)\n\}", html, re.DOTALL)
    assert match, "rerender must be defined as a top-level function"
    assert "Date.now()" in match.group(1)
    assert "latestTraps" in match.group(1)
    assert "latestReadsByNode" in match.group(1)


def test_the_threshold_slider_value_is_never_sent_in_a_network_request() -> None:
    """design.md 3.2's amendment / the user's own words on delivery: the
    "still here" threshold is a viewer control -- it must never leave the
    browser. Every `fetch(` call site in the page is checked; none may
    reference the slider's value or its backing variable.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")

    fetch_calls = re.findall(r"fetch\([^)]*\)", html)
    assert fetch_calls, "the page must still call fetch() for its existing routes"
    assert not any("threshold" in call.lower() for call in fetch_calls), (
        "no fetch() call may carry the viewer's threshold -- it is never sent to the station"
    )


def test_an_untrusted_clock_read_group_shows_a_caveat_not_a_confident_time() -> None:
    """A read whose `node_epoch` is `null` (phase 2's own masking, an
    untrusted node clock) must not render as if its time were as certain
    as a trusted one -- the page must not invent a confirmed-looking time
    for it either.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")

    assert re.search(
        r'g\.lastEpochEstimated\s*\?\s*\'[^\']*reloj del nodo sin confirmar[^\']*\'\s*:\s*""',
        html,
    )


def test_reads_section_renders_a_fallback_with_no_reads_at_all() -> None:
    """First render, a freshly booted station: `/api/reads` answers `{}`
    (or a node with no rows yet) and the page must say so in Spanish, the
    same "Ninguna trampa..." empty-state precedent `renderTraps` already
    sets, rather than an empty `<div>` a person could mistake for a
    loading glitch.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")

    assert "Sin lecturas recientes." in html


def test_refresh_also_fetches_the_reads_window() -> None:
    """`refresh()` must pull `/api/reads` alongside the existing
    `/api/traps` and `/health` calls -- fetch-on-notice (this project's own
    SSE publishes a bare `{"type": "traps_changed"}`, never the read data
    itself), not a second streaming channel.
    """
    html = STATIC_INDEX_PATH.read_text(encoding="utf-8")

    assert re.search(r'fetch\("/api/reads"\)', html)


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
    ingest a frame (the gateway thread, slices 5/6), poll a node's backlog
    (slice 7's `poll_node`, paced by `run_scheduler_loop` -- corrected
    here, CRITICAL 5/WARNING 14: this used to say "run a rotation round,"
    the deleted `run_rotation_round` primitive, not what this loop calls),
    and resend a due command (slice 8), end to end. This is the test that
    proves all four orphaned drivers this slice exists to start actually
    run: the fourth, `attempt_time_sync` (slice 9), also fires every tick
    here since the clock is trusted, but is not separately asserted on --
    TIME_SYNC has no user-facing effect this test can read back other than
    one more frame type on the wire.
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
        registry=registry,
        clock=_FixedClock(ClockTrust.NTP),
        store=store,
        gateway=gateway,
        # CRITICAL 3 (sdd-verify field-station-p4): this test is about the
        # scheduler's *wiring* (does it run at all), not the shared
        # station-wide budget -- a generous rate keeps it from depending on
        # whether node 11's async ingest beats the first tick and briefly
        # pushes TIME_SYNC demand above the default budget's capacity; the
        # budget itself is proven for real in tests/test_cli.py.
        config=StationConfig(channel_budget_frames_per_s=1000.0),
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
    assert payloads.GET_BACKLOG in written_types, (
        "the scheduler thread never polled a node's backlog"
    )
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


# --- network-gateway: /health must not flatten many nodes into one flag ----


def test_health_reports_the_network_transport_with_one_row_per_node(tmp_path: Path) -> None:
    """The 1:N sibling of ``test_link_drop_surfaces_in_health`` above: a
    ``NetworkGateway`` configured instead of a ``SerialGateway`` must not
    make ``/health`` fall back to a single ``connected`` boolean that
    cannot say *which* of several nodes is missing (docs/pending.md's
    closing rule, this change's own report).
    """
    store = Store(tmp_path / "station.db")
    gateway = NetworkGateway(NetworkGatewayConfig(host="127.0.0.1", port=0))
    app = create_app(store=store, gateway=gateway)

    with TestClient(app) as c:
        deadline = time.monotonic() + 2.0
        port = gateway.bound_port
        while port is None and time.monotonic() < deadline:
            time.sleep(0.02)
            port = gateway.bound_port
        assert port is not None

        body = c.get("/health").json()
        assert body["link"]["transport"] == "network"
        assert body["link"]["listening"] is True
        assert body["link"]["nodes"] == []
        # A listening socket with nobody connected yet is still a working
        # transport -- the same "never enabled, never counts" honesty
        # every other `/health` field already applies; the absence of
        # traps is a roster fact, not a transport failure.
        assert body["ok"] is True

        client_a = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        client_b = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        try:
            for sock, node_id in ((client_a, 501), (client_b, 502)):
                frame = frames.encode(Frame(msg_type=frames.PING, node_id=node_id))
                sock.sendall(cobs_encode(frame) + bytes([DELIMITER]))

            def _both_identified() -> bool:
                nonlocal body
                body = c.get("/health").json()
                return len(body["link"]["nodes"]) == 2

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not _both_identified():
                time.sleep(0.02)
            assert len(body["link"]["nodes"]) == 2
            assert {n["connected"] for n in body["link"]["nodes"]} == {True}

            client_b.close()

            def _one_dropped() -> bool:
                nonlocal body
                body = c.get("/health").json()
                by_id = {n["node_id"]: n["connected"] for n in body["link"]["nodes"]}
                return by_id.get(502) is False

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not _one_dropped():
                time.sleep(0.02)

            by_id = {n["node_id"]: n["connected"] for n in body["link"]["nodes"]}
            # The exact defect this change's report names: node 501 still
            # connected must never be allowed to hide node 502 being gone.
            assert by_id == {501: True, 502: False}
        finally:
            client_a.close()
            client_b.close()


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


# --- 13.3/13.4: ARM/DISARM are recorded, not only applied in memory -------


def test_arm_is_recorded_as_an_operator_event(tmp_path: Path) -> None:
    """event-store spec.md, "Capture persists until acknowledged" -- the
    same store-write-before-registry discipline this slice applies to
    disarm/edit-targets/acknowledge/cancel, now proven for arm again.
    """
    store = Store(tmp_path / "station.db")
    c = TestClient(create_app(store=store))

    assert c.post("/api/traps/7/arm").status_code == 200

    armed = store.latest(7, OPERATOR_ARM)
    assert armed is not None
    assert armed.source == "operator"


def test_disarm_is_recorded_as_an_operator_event(tmp_path: Path) -> None:
    store = Store(tmp_path / "station.db")
    c = TestClient(create_app(store=store))

    assert c.post("/api/traps/7/disarm").status_code == 200

    disarmed = store.latest(7, OPERATOR_DISARM)
    assert disarmed is not None
    assert disarmed.source == "operator"


def test_cancel_command_is_recorded_as_an_operator_event(tmp_path: Path) -> None:
    """event-store spec.md's own discipline, extended to cancellation: an
    operator cancelling a command is durable, the same as arm/disarm/
    edit-targets/acknowledge already are -- never only an in-memory flag.
    """
    store = Store(tmp_path / "station.db")
    c = TestClient(create_app(store=store))

    assert c.post("/api/traps/7/disarm").status_code == 200
    assert c.post("/api/traps/7/cancel").status_code == 200

    cancelled = store.latest(7, OPERATOR_CANCEL_COMMAND)
    assert cancelled is not None
    assert cancelled.source == "operator"


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
    assert c.post("/api/traps/7/cancel").status_code == 200


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
