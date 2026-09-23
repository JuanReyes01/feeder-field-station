"""``cli.py`` — 12.8: the args a systemd-managed deployment needs to point
at real on-disk state and a real (or deliberately absent) serial device,
plus the rollback kill switch (`--no-gateway`) design.md names as the
concurrency section's own escape hatch: "the gateway is off by default...
turned on by cli.py when a device is configured."

``build_app`` is what `main()` calls before handing off to `uvicorn.run` --
pulled out so these tests can construct the real app and inspect what each
flag produced without actually starting a server.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from feeder_station.adapters.ingest import _apply_to_registry
from feeder_station.adapters.store import Store
from feeder_station.cli import build_app
from feeder_station.config import StationConfig
from feeder_station.domain import frames, payloads
from feeder_station.domain.events import OPERATOR_SET_TARGETS, StationEvent, interpret
from feeder_station.domain.traps import Registry

TAG = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])


def test_db_path_flag_is_used_to_open_the_store(tmp_path: Path) -> None:
    db_path = tmp_path / "custom-station.db"

    app, args = build_app(["--db-path", str(db_path), "--no-gateway"])

    assert args.db_path == str(db_path)
    assert app.state.store is not None
    assert db_path.exists()  # Store.__init__ creates the file on open


def test_no_gateway_flag_starts_with_no_gateway_configured(tmp_path: Path) -> None:
    app, args = build_app(["--db-path", str(tmp_path / "station.db"), "--no-gateway"])

    assert args.no_gateway is True
    assert app.state.gateway is None


def test_serial_device_flag_is_passed_through_to_the_gateway(tmp_path: Path) -> None:
    app, args = build_app(
        [
            "--db-path",
            str(tmp_path / "station.db"),
            "--serial-device",
            "/dev/ttyUSB0",
        ]
    )

    assert args.no_gateway is False
    assert app.state.gateway is not None
    assert app.state.gateway.resolve() == "/dev/ttyUSB0"


def test_without_no_gateway_a_gateway_is_still_constructed_even_with_no_device(
    tmp_path: Path,
) -> None:
    """No `--serial-device` and no `--no-gateway` is a real deployment
    shape: hardware not plugged in yet, or resolved entirely by `by-id`/
    VID:PID at a later revision of this flag set. The gateway must still
    come up and simply report the link down (`adapters/serial_gateway.py`
    step 4), never crash the whole station over a missing radio.
    """
    app, args = build_app(["--db-path", str(tmp_path / "station.db")])

    assert args.no_gateway is False
    assert app.state.gateway is not None
    assert app.state.gateway.resolve() is None


def test_station_config_flags_share_the_same_argv(tmp_path: Path) -> None:
    """test_config.py's own precedent: a later slice's cli.py flags must
    share `argv` with `StationConfig.load` without either parser choking on
    the other's flags, and `build_app` must actually hand that parsed
    config to `create_app` -- not a fresh, flag-less default.
    """
    argv = [
        "--db-path",
        str(tmp_path / "station.db"),
        "--no-gateway",
        "--backlog-retry-budget",
        "5",
    ]

    app, _args = build_app(argv)

    expected_config = StationConfig.load(argv)
    assert app.state.config.backlog_retry_budget == 5
    assert app.state.config == expected_config


# --- CRITICAL 1/2/3rd replay step (sdd-verify, field-station-p4) -----------
#
# All three exercised through `build_app`, the real ExecStart path
# (`deploy/feeder-station.service`), never through a hand-called
# `store.rehydrate()`. Task 10.6 was marked done against a test
# (`test_unacknowledged_capture_is_present_after_a_simulated_restart`) that
# rehydrated the registry itself and handed the populated result to
# `create_app` -- the test built the wiring production lacked. These do not.


def test_rehydration_runs_through_the_real_boot_path(tmp_path: Path) -> None:
    """CRITICAL 1: `Store.rehydrate()` had no production caller at all --
    `create_app` built an empty `Registry` and nothing in `src/` ever
    replayed the store into it. A station restarted with an unacknowledged
    capture served an empty `/api/traps`, contradicting operator-ui/spec.md
    ("An unacknowledged capture MUST remain visible across ... station
    restarts") and S3's own "still there for anyone who opens the
    interface hours later."
    """
    db_path = tmp_path / "station.db"
    seed = Store(db_path)
    seed.append(
        StationEvent(
            source="node",
            node_id=7,
            msg_type=payloads.CAPTURED,
            msg_id=1,
            node_epoch=None,
            tag=TAG,
            fields={"state_after": 2},
            payload=b"",
        ),
        received_epoch=1000.0,
        clock_trust="unset",
    )
    seed.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])

    with TestClient(app) as c:
        rows = {row["node_id"]: row for row in c.get("/api/traps").json()}

    assert 7 in rows, "the registry was never rehydrated -- the capture in station.db is invisible"
    assert rows[7]["capture"] is not None
    assert rows[7]["capture"]["tag"] == "0384199C87EC06"


def test_an_older_capture_does_not_regress_a_newer_heartbeats_state(tmp_path: Path) -> None:
    """CRITICAL 2: `rehydrate()` ran `_rehydrate_heartbeat` then
    `_rehydrate_capture` unconditionally, and the old `Registry.captured()`
    call stamped both `state` and `last_seen` from whichever CAPTURED row
    was stored, however old. A trap captured hours ago that has since
    reported healthy, over and over, came back from *every* restart
    reported `fired` and unreachable -- a false fault (S4/S11). Must ship
    together with CRITICAL 1: wiring `rehydrate()` into `create_app` alone,
    without this, would have shipped this regression live.
    """
    db_path = tmp_path / "station.db"
    seed = Store(db_path)
    now_epoch = datetime.now(UTC).timestamp()
    capture_epoch = now_epoch - 6 * 3600  # captured six hours ago
    heartbeat_epoch = now_epoch - 120  # reported healthy two minutes ago

    seed.append(
        StationEvent(
            source="node",
            node_id=7,
            msg_type=payloads.CAPTURED,
            msg_id=1,
            node_epoch=None,
            tag=TAG,
            fields={"state_after": 2},
            payload=b"",
        ),
        received_epoch=capture_epoch,
        clock_trust="unset",
    )
    seed.append(
        StationEvent(
            source="node",
            node_id=7,
            msg_type=payloads.HEARTBEAT,
            msg_id=2,
            node_epoch=None,
            tag=None,
            fields={"state": 0, "battery_mv": 4000, "targets_version": 1},  # 0 -> IDLE
            payload=b"",
        ),
        received_epoch=heartbeat_epoch,
        clock_trust="ntp",
    )
    seed.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])

    with TestClient(app) as c:
        rows = {row["node_id"]: row for row in c.get("/api/traps").json()}

    trap = rows[7]
    assert trap["state"] == "idle"  # the trap's own latest report, never the older capture
    assert trap["reachable"] is True  # last_seen is the heartbeat's, not the six-hour-old capture's
    assert trap["capture"]["tag"] == "0384199C87EC06"  # S3 still holds: still surfaced


# --- CRITICAL 1 (sdd-verify, field-station-p4 batch 2): the rehydrated
# registry must equal the live registry, for every ordering and every
# report type -- verify-report-2.md's own diagnosis is that the prior fix
# was validated against one example instead of this rule. --------------------

_REHYDRATION_NODE = 11
_OLD = 6 * 3600.0
_NEW = 120.0
_NEWER = 60.0
_RECENT = 30.0

# (msg_type, fields, tag, seconds-before-now) per event, oldest listed first
# only by convention -- the property under test does not assume an order.
_REHYDRATION_ORDERINGS: list[
    tuple[str, list[tuple[int, dict[str, object], bytes | None, float]]]
] = [
    (
        "capture_old_heartbeat_new",
        [
            (payloads.CAPTURED, {"state_after": 2}, TAG, _OLD),
            (
                payloads.HEARTBEAT,
                {"state": 0, "battery_mv": 4000, "targets_version": 1},
                None,
                _NEW,
            ),
        ],
    ),
    (
        "heartbeat_old_capture_new",  # the mirror ordering the prior fix regressed
        [
            (
                payloads.HEARTBEAT,
                {"state": 1, "battery_mv": 3900, "targets_version": 2},
                None,
                _OLD,
            ),
            (payloads.CAPTURED, {"state_after": 2}, TAG, _NEW),
        ],
    ),
    (
        "capture_alone_no_heartbeat_ever",
        [(payloads.CAPTURED, {"state_after": 2}, TAG, _RECENT)],
    ),
    (
        "heartbeat_then_fault",
        [
            (
                payloads.HEARTBEAT,
                {"state": 1, "battery_mv": 3900, "targets_version": 2},
                None,
                _OLD,
            ),
            (payloads.FAULT, {"code": 1, "detail": ""}, None, _NEWER),
        ],
    ),
    (
        "fault_then_newer_heartbeat",  # the un-reported mirror of the case above
        [
            (payloads.FAULT, {"code": 1, "detail": ""}, None, _OLD),
            (
                payloads.HEARTBEAT,
                {"state": 1, "battery_mv": 3900, "targets_version": 2},
                None,
                _NEW,
            ),
        ],
    ),
]


@pytest.mark.parametrize(
    ("case_id", "events"),
    _REHYDRATION_ORDERINGS,
    ids=[case_id for case_id, _ in _REHYDRATION_ORDERINGS],
)
def test_rehydrated_registry_matches_the_live_registry_for_every_ordering(
    tmp_path: Path,
    case_id: str,
    events: list[tuple[int, dict[str, object], bytes | None, float]],
) -> None:
    """CRITICAL 1 (verify-report-2.md, Part 1.2): not "this ordering
    rehydrates correctly" but *the rehydrated registry equals the live
    registry, for every ordering and every report type* -- `HEARTBEAT`,
    `CAPTURED` and `FAULT`, in both directions. One property, one test,
    parametrized over five logs: the same log fed through two paths (the
    live ingest path, replayed directly here via `adapters/ingest.py::
    _apply_to_registry`; the rehydrated path, through a real restart) must
    agree, never merely "the one ordering a report happened to print."

    `event-store/spec.md` "Registry rehydration at boot" (rebuild the
    last known reported state); `radio-gateway/spec.md` "FAULT and
    silence handling" (a `FAULT` MUST update reported state, including
    across a restart); `trap-commands/spec.md`'s S4 ("displayed state
    MUST always be whatever its own most recent report said"); S3, S4,
    S11.
    """
    now = datetime.now(UTC)
    now_epoch = now.timestamp()

    db_path = tmp_path / "station.db"
    seed = Store(db_path)
    live_registry = Registry(silence_after=timedelta(minutes=15))
    for msg_id, (msg_type, fields, tag, offset) in enumerate(events, start=1):
        event = StationEvent(
            source="node",
            node_id=_REHYDRATION_NODE,
            msg_type=msg_type,
            msg_id=msg_id,
            node_epoch=None,
            tag=tag,
            fields=fields,
            payload=b"",
        )
        received_epoch = now_epoch - offset
        seed.append(event, received_epoch=received_epoch, clock_trust="ntp")
        _apply_to_registry(live_registry, event, at=datetime.fromtimestamp(received_epoch, tz=UTC))
    seed.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])
    with TestClient(app) as c:
        rehydrated_rows = {row["node_id"]: row for row in c.get("/api/traps").json()}

    live_row = live_registry.view(now=now)[0]
    rehydrated_row = rehydrated_rows[_REHYDRATION_NODE]

    for field in ("state", "last_seen", "reachable"):
        assert rehydrated_row[field] == live_row[field], (
            f"{case_id}: {field} diverges -- live={live_row[field]!r} "
            f"rehydrated={rehydrated_row[field]!r}"
        )


# --- CRITICAL A (verify-report-batch2a.md): the ordering axis above holds
# `state_after` fixed at 2 (FIRED) in all five logs -- the one value where
# the old hardcoded live `FIRED` and the rehydrated `state_after`-sourced
# fact coincide. This varies the *content* axis instead, ordering fixed at
# "CAPTURED is the node's only report". Kept separate from
# `_REHYDRATION_ORDERINGS` rather than crossed with it: only one of the
# five orderings there ever has CAPTURED win the race, so crossing all
# five against four `state_after` values would add cases where the
# content axis has no effect, proving nothing this smaller test does not
# already prove. Built from real wire bytes (`frames.encode` ->
# `frames.decode` -> `events.interpret`), matching verify-report-batch2a.md's
# own Part 1.2 re-measurement methodology. ------------------------------

_CRITICAL_A_NODE = 21


@pytest.mark.parametrize(
    ("state_after", "expected_state"),
    [(0, "idle"), (1, "armed"), (2, "fired"), (3, "fault")],
)
def test_rehydrated_registry_matches_the_live_registry_for_every_captured_state_after(
    tmp_path: Path, state_after: int, expected_state: str
) -> None:
    """CRITICAL A (verify-report-batch2a.md): before this fix, the live
    `Registry.captured()` hardcoded `FIRED` regardless of what the node
    reported, while the rehydrated `_rehydrate_capture` already read
    `state_after` (WARNING 7) -- the same log produced two different
    answers depending on whether the station had restarted. Only
    `state_after=2` ever agreed by coincidence, exactly why the ordering
    cases above never noticed.
    """
    payload = TAG + bytes([0x00, 0x00, 0x00, 0x00, state_after])  # epoch=0 + state_after
    raw_frame = frames.encode(
        frames.Frame(
            msg_type=payloads.CAPTURED, node_id=_CRITICAL_A_NODE, msg_id=1, payload=payload
        )
    )
    event = interpret(frames.decode(raw_frame))

    now = datetime.now(UTC)
    received_epoch = now.timestamp()
    at = datetime.fromtimestamp(received_epoch, tz=UTC)

    live_registry = Registry(silence_after=timedelta(minutes=15))
    _apply_to_registry(live_registry, event, at=at)

    db_path = tmp_path / "station.db"
    seed = Store(db_path)
    seed.append(event, received_epoch=received_epoch, clock_trust="ntp")
    seed.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])
    with TestClient(app) as c:
        rehydrated_rows = {row["node_id"]: row for row in c.get("/api/traps").json()}

    live_row = live_registry.view(now=now)[0]
    rehydrated_row = rehydrated_rows[_CRITICAL_A_NODE]

    # Both must honor state_after -- which also proves they agree with each
    # other, the CRITICAL 1 invariant this file exists to hold.
    assert live_row["state"] == expected_state, f"live: got {live_row['state']!r}"
    assert rehydrated_row["state"] == expected_state, f"rehydrated: got {rehydrated_row['state']!r}"


# --- CRITICAL 2 (sdd-verify, field-station-p4): a corrupt stored row must
# never brick the boot -------------------------------------------------------


def test_boot_survives_an_out_of_range_stored_heartbeat_state(tmp_path: Path) -> None:
    """Before this fix, `store.rehydrate()` ran inside `create_app`'s
    lifespan with no `try`. `adapters/store.py::trap_state_from_wire` raises
    `ValueError` for any `state` byte outside 0-3; nothing caught it, so the
    ASGI startup raised, `uvicorn` exited, and `Restart=always`
    (`deploy/feeder-station.service`) restarted straight into the same
    unreadable row -- an infinite crash loop, no `/health`, no UI,
    unrecoverable in the field without hand-editing SQLite.

    The row here is written directly through `Store.append`, bypassing
    `payloads.decode_heartbeat`'s now-fixed boundary check (task 1.2) --
    simulating a bit-flip that happened before that check existed, or any
    future row the boundary did not anticipate. `event-store/spec.md`'s
    "Registry rehydration at boot" requirement is that a rebuild must not
    abort the whole station over one row; `station-deployment/spec.md`'s S7
    ("starts by itself, a power cut must not need a person") is what a
    crash loop directly violates. A boot that cannot read one row must
    still be a boot: it must reach `/health`, and the read failure must be
    visible there, not merely swallowed.
    """
    db_path = tmp_path / "station.db"
    seed = Store(db_path)
    seed.append(
        StationEvent(
            source="node",
            node_id=7,
            msg_type=payloads.HEARTBEAT,
            msg_id=1,
            node_epoch=None,
            tag=None,
            fields={"state": 7, "battery_mv": 4000, "targets_version": 1},  # 7: no known TrapState
            payload=b"",
        ),
        received_epoch=1000.0,
        clock_trust="unset",
    )
    seed.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])

    with TestClient(app) as c:
        response = c.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["rehydration_errors"] == 1


# --- CRITICAL A (verify-report-batch1.md): `state=7` above is only one
# member of the family `except ValueError` missed -- these drive the other
# two (`OverflowError`, `AttributeError`) plus the roster query itself.


def _seed_valid_heartbeat(store: Store) -> int:
    appended = store.append(
        StationEvent(
            source="node",
            node_id=7,
            msg_type=payloads.HEARTBEAT,
            msg_id=1,
            node_epoch=None,
            tag=None,
            fields={"state": 0, "battery_mv": 4000, "targets_version": 1},
            payload=b"",
        ),
        received_epoch=1000.0,
        clock_trust="unset",
    )
    return appended.seq


def _corrupt_received_epoch_overflow(store: Store, seq: int) -> None:
    store._conn.execute("UPDATE event SET received_epoch = 1e20 WHERE seq = ?", (seq,))


def _corrupt_fields_not_an_object(store: Store, seq: int) -> None:
    store._conn.execute("UPDATE event SET fields = 'null' WHERE seq = ?", (seq,))


@pytest.mark.parametrize(
    "corrupt",
    [_corrupt_received_epoch_overflow, _corrupt_fields_not_an_object],
    ids=["received_epoch_overflow", "fields_not_an_object"],
)
def test_boot_survives_a_non_value_error_corrupt_row(tmp_path: Path, corrupt: object) -> None:
    """P1/P2: written directly, bypassing `Store.append`, simulating a
    bit-flip landing on an already-stored row (S7).
    """
    db_path = tmp_path / "station.db"
    seed = Store(db_path)
    seq = _seed_valid_heartbeat(seed)
    corrupt(seed, seq)  # type: ignore[operator]
    seed.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])

    with TestClient(app) as c:
        response = c.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["rehydration_errors"] == 1


def test_boot_survives_a_broken_node_roster_query(tmp_path: Path) -> None:
    """`_known_node_ids()`'s query and the loop over it used to sit above
    the per-step `try` entirely. Dropping the table is a deterministic
    stand-in for corruption landing on it.
    """
    db_path = tmp_path / "station.db"
    Store(db_path).close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])
    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("DROP TABLE node_cursor")
    raw.close()

    with TestClient(app) as c:
        response = c.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["rehydration_errors"] == 1


# --- WARNING J (verify-report-batch1-round2.md): a row rehydration cannot
# read must never fabricate "idle" -- "unknown" must be representable. ------


def test_a_node_with_no_readable_state_source_shows_unknown_not_idle(tmp_path: Path) -> None:
    """`battery_mv`/`targets_version`/`last_seen` all honestly render
    `null` when their source row cannot be read; before this fix `state`
    was the one field that invented a value instead -- `Trap`'s dataclass
    default, `TrapState.IDLE`, the calmest reading available and
    indistinguishable from a trap that genuinely reported idle. In a
    system whose whole job is telling a biologist whether a trap is
    holding an animal, that is the worst possible default
    (verify-report-batch1-round2.md: "a false all-clear on a trap holding
    an animal").

    The only `HEARTBEAT` row is corrupted the same way CRITICAL 2's own
    tests corrupt one (`state` byte out of range); the `CAPTURED` row is
    otherwise well-formed but its own `state_after` is missing -- a row
    shape the wire decoder could never produce (`decode_captured`
    requires it), simulating corruption landing on that one field
    specifically, the same technique WARNING B's own parametrized
    `payloads.py` tests use. Nothing anywhere can honestly supply a state
    fact for this node; the capture object itself is still restored (S3).
    """
    db_path = tmp_path / "station.db"
    seed = Store(db_path)
    seed.append(
        StationEvent(
            source="node",
            node_id=7,
            msg_type=payloads.HEARTBEAT,
            msg_id=1,
            node_epoch=None,
            tag=None,
            fields={"state": 7, "battery_mv": 3100, "targets_version": 9},  # 7: no known TrapState
            payload=b"",
        ),
        received_epoch=1000.0,
        clock_trust="unset",
    )
    seed.append(
        StationEvent(
            source="node",
            node_id=7,
            msg_type=payloads.CAPTURED,
            msg_id=2,
            node_epoch=None,
            tag=TAG,
            fields={},  # state_after missing -- simulated corruption of that field alone
            payload=b"",
        ),
        received_epoch=1500.0,
        clock_trust="unset",
    )
    seed.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])
    with TestClient(app) as c:
        rows = {row["node_id"]: row for row in c.get("/api/traps").json()}
        health = c.get("/health").json()

    trap = rows[7]
    assert trap["state"] is None  # honest "we do not know" -- never a fabricated "idle"
    assert trap["capture"]["tag"] == "0384199C87EC06"  # S3 still holds
    assert health["rehydration_errors"] == 1  # only the heartbeat row was unreadable


def test_an_unconfirmed_set_targets_command_survives_a_restart(tmp_path: Path) -> None:
    """design.md section 6's third replay step. Before this fix, an edit
    (`PUT /targets`) issued between a trap's last heartbeat and a restart
    was durable in `station.db` (slice 13 proved that) but silently
    dropped from the registry on the next boot -- nothing ever resent it,
    even though the trap itself never confirmed the new version.
    """
    db_path = tmp_path / "station.db"
    seed = Store(db_path)
    seed.append(
        StationEvent(
            source="node",
            node_id=5,
            msg_type=payloads.HEARTBEAT,
            msg_id=1,
            node_epoch=None,
            tag=None,
            fields={"state": 0, "battery_mv": 4000, "targets_version": 1},
            payload=b"",
        ),
        received_epoch=1000.0,
        clock_trust="ntp",
    )
    seed.append(
        StationEvent(
            source="operator",
            node_id=5,
            msg_type=OPERATOR_SET_TARGETS,
            msg_id=None,
            node_epoch=None,
            tag=None,
            fields={"targets_version": 2, "tags": ["0384199C87EC06"]},
            payload=None,
        ),
        received_epoch=1500.0,
        clock_trust="ntp",
    )
    seed.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])

    with TestClient(app) as c:
        rows = {row["node_id"]: row for row in c.get("/api/traps").json()}

    trap = rows[5]
    assert trap["pending_command"] == "set_targets"
    assert trap["desired_targets_version"] == 2
    assert trap["targets"] == ["0384199C87EC06"]


def test_a_confirmed_set_targets_command_is_not_resurrected_on_restart(tmp_path: Path) -> None:
    """The other half of DD15 applied to rehydration: a command the trap
    already confirmed *before* the restart must not come back as pending
    just because its store row is still the latest operator event for that
    node -- only ``desired_tags`` (S2, independent of pending status)
    should still show.
    """
    db_path = tmp_path / "station.db"
    seed = Store(db_path)
    seed.append(
        StationEvent(
            source="operator",
            node_id=5,
            msg_type=OPERATOR_SET_TARGETS,
            msg_id=None,
            node_epoch=None,
            tag=None,
            fields={"targets_version": 2, "tags": ["0384199C87EC06"]},
            payload=None,
        ),
        received_epoch=1000.0,
        clock_trust="ntp",
    )
    # The trap's own heartbeat, sent AFTER the edit, already reports v2 --
    # the command is satisfied (DD15) before the station ever restarts.
    seed.append(
        StationEvent(
            source="node",
            node_id=5,
            msg_type=payloads.HEARTBEAT,
            msg_id=1,
            node_epoch=None,
            tag=None,
            fields={"state": 0, "battery_mv": 4000, "targets_version": 2},
            payload=b"",
        ),
        received_epoch=1500.0,
        clock_trust="ntp",
    )
    seed.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])

    with TestClient(app) as c:
        rows = {row["node_id"]: row for row in c.get("/api/traps").json()}

    trap = rows[5]
    assert trap["pending_command"] is None  # already confirmed -- nothing left to resend
    assert trap["targets"] == ["0384199C87EC06"]  # desired_tags still shown (S2), regardless
