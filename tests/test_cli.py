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

import dataclasses
import logging
import os
import pty
import select
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from feeder_station.adapters.ingest import _apply_to_registry
from feeder_station.adapters.serial_gateway import SerialGateway
from feeder_station.adapters.store import Store, trap_state_from_wire
from feeder_station.cli import build_app
from feeder_station.config import DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S_LORA, StationConfig
from feeder_station.domain import frames, payloads
from feeder_station.domain.events import (
    OPERATOR_ARM,
    OPERATOR_DISARM,
    OPERATOR_SET_TARGETS,
    STATION_TIME_SYNC_WITHHELD,
    StationEvent,
    interpret,
)
from feeder_station.domain.serial_framing import cobs_decode
from feeder_station.domain.traps import Registry, Trap

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


def test_transport_defaults_to_serial_so_the_bench_cable_keeps_working(tmp_path: Path) -> None:
    """``serial`` is the only transport this station has: the trap side
    already talks ESP-NOW to a gateway ESP32, which then talks USB serial
    to this station -- omitting ``--transport`` entirely must construct
    exactly the same ``SerialGateway`` it always has.
    """
    app, args = build_app(
        ["--db-path", str(tmp_path / "station.db"), "--serial-device", "/dev/ttyUSB0"]
    )

    assert args.transport == "serial"
    assert isinstance(app.state.gateway, SerialGateway)


def test_transport_rejects_the_old_network_choice_with_a_clear_error(tmp_path: Path) -> None:
    """The Wi-Fi/TCP node transport (``--transport network``) is gone: an
    ESP32 that associates with a Wi-Fi AP has its radio channel dragged to
    the AP's own channel (Espressif's "home channel" rule), the same radio
    ESP-NOW needs, and the site's Starlink network gives this station no
    control over that channel or its stability.

    ``--transport`` keeps its one remaining choice, ``serial``, rather
    than disappearing outright -- ``build_app`` parses with
    ``parse_known_args``, which silently ignores a flag it does not
    recognise at all; keeping ``--transport`` with a single valid choice
    means an operator who still types ``--transport network`` out of
    habit gets argparse's own "invalid choice" error instead of the flag
    quietly doing nothing.
    """
    with pytest.raises(SystemExit):
        build_app(
            ["--db-path", str(tmp_path / "station.db"), "--transport", "network"]
        )


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

# BEHAVIOUR 4 (verify-report-batch2d.md): the comparison axis below used to
# be a hand-picked tuple, `("state", "last_seen", "reachable")` -- and
# `pending_command` was on `Trap` from the very first version of this
# module, yet never made it onto that tuple, which is exactly why the
# divergence this file's own BEHAVIOUR-4 test proves hid for six rounds.
# Enumerated from `Trap`'s own dataclass definition instead, so a field
# added there later cannot silently fall off this axis: `assert not
# missing` below fails the whole module at import time, not quietly.
_TRAP_FIELD_TO_VIEW_KEYS: dict[str, tuple[str, ...]] = {
    "node_id": ("node_id",),
    "state": ("state",),
    "state_may_be_stale": ("state_may_be_stale",),
    "battery_mv": ("battery_mv",),
    "targets_version": ("targets_version",),
    "last_seen": ("last_seen", "reachable", "trustworthy"),
    "pending_command": ("pending_command", "desired_targets_version"),
    "capture": ("capture",),
    "desired_tags": ("targets",),
    "armed_via_remote_command_at": ("armed_via_remote_command_at",),
    # GET_TARGETS/TARGETS (docs/protocol/messages.md, added 2026-09-27):
    "reported_targets_version": ("reported_targets_version",),
    "reported_tags": ("reported_targets", "targets_match"),
    # `targets_reassembly` has no view() key of its own -- it is a transient,
    # in-progress buffer (`domain/targets_reassembly.py`), never rendered
    # directly -- but it shares "is a fetch currently under way" with
    # `pending_targets_fetch`, which does have one.
    "targets_reassembly": ("targets_fetch_pending",),
    "pending_targets_fetch": ("targets_fetch_pending",),
}


# PROOF 1 (verify-report-final.md): `Registry.view()`'s own 12 emitted keys,
# named once here from the method's source (domain/traps.py) so the mapping
# below is checked against what view() actually emits, not merely against
# `Trap`'s own field names -- a mapping present but empty, or present but
# pointing at the wrong key, satisfied the old guard just as well as a
# correct one.
_VIEW_KEYS = frozenset(
    {
        "node_id",
        "state",
        "state_may_be_stale",
        "reachable",
        "trustworthy",
        "battery_mv",
        "targets_version",
        "pending_command",
        "desired_targets_version",
        "targets",
        "last_seen",
        "capture",
        "armed_via_remote_command_at",
        "reported_targets",
        "reported_targets_version",
        "targets_match",
        "targets_fetch_pending",
    }
)


def _compared_view_fields() -> tuple[str, ...]:
    trap_field_names = {f.name for f in dataclasses.fields(Trap)}
    missing = trap_field_names - set(_TRAP_FIELD_TO_VIEW_KEYS)
    assert not missing, f"Trap field(s) {missing} have no view()-key mapping -- add one above"
    empty = [field for field, keys in _TRAP_FIELD_TO_VIEW_KEYS.items() if not keys]
    assert not empty, (
        f"Trap field(s) {empty} map to an empty tuple -- PROOF 1 (verify-report-final.md): "
        "a present-but-empty mapping satisfied the old guard while dropping the field from "
        "the comparison axis entirely"
    )
    compared = tuple(key for keys in _TRAP_FIELD_TO_VIEW_KEYS.values() for key in keys)
    assert set(compared) == _VIEW_KEYS, (
        f"mapped key(s) {set(compared) ^ _VIEW_KEYS} do not match Registry.view()'s own "
        "emitted keys exactly -- PROOF 1: a mapping naming a real but wrong key (e.g. "
        "another field's key) is otherwise indistinguishable from a correct one"
    )
    return compared


_COMPARED_VIEW_FIELDS = _compared_view_fields()

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

    for field in _COMPARED_VIEW_FIELDS:
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


# --- BEHAVIOUR 4 / SUGGESTION 5 (verify-report-batch2d.md): live and
# rehydrated must agree on whether a report satisfies a *pending command*,
# not only on `state`/`last_seen`/`reachable` -- `Registry.heartbeat()` ran
# `satisfied_by`; `captured()`/`fault()` did not, so the same log gave two
# different answers for `pending_command` depending on whether the station
# had restarted. None of the five `_REHYDRATION_ORDERINGS` logs above has an
# operator row at all, which is exactly why this hid for six rounds. Measured:
# 5 of 18 combinations diverged, always by rehydration silently dropping a
# command the live path kept pending. This table is those same 2 pending
# commands (arm, disarm) x 9 report shapes (4 HEARTBEAT states, 4 CAPTURED
# state_afters, 1 FAULT) = 18, re-derived here as a permanent property rather
# than a one-off repro. -------------------------------------------------------

_BEHAVIOUR_4_NODE = 33

# (case_id, msg_type, fields, tag) per report shape.
_BEHAVIOUR_4_REPORT_SHAPES: list[tuple[str, int, dict[str, object], bytes | None]] = [
    (
        "heartbeat_idle",
        payloads.HEARTBEAT,
        {"state": 0, "battery_mv": 4000, "targets_version": 1},
        None,
    ),
    (
        "heartbeat_armed",
        payloads.HEARTBEAT,
        {"state": 1, "battery_mv": 4000, "targets_version": 1},
        None,
    ),
    (
        "heartbeat_fired",
        payloads.HEARTBEAT,
        {"state": 2, "battery_mv": 4000, "targets_version": 1},
        None,
    ),
    (
        "heartbeat_fault",
        payloads.HEARTBEAT,
        {"state": 3, "battery_mv": 4000, "targets_version": 1},
        None,
    ),
    ("captured_idle", payloads.CAPTURED, {"state_after": 0}, TAG),
    ("captured_armed", payloads.CAPTURED, {"state_after": 1}, TAG),
    ("captured_fired", payloads.CAPTURED, {"state_after": 2}, TAG),
    ("captured_fault", payloads.CAPTURED, {"state_after": 3}, TAG),
    ("fault", payloads.FAULT, {"code": 1, "detail": ""}, None),
]


def _apply_operator_command_to_registry(registry: Registry, command: str, at: datetime) -> None:
    if command == "arm":
        registry.command_arm(_BEHAVIOUR_4_NODE, at=at)
    else:
        registry.command_disarm(_BEHAVIOUR_4_NODE, at=at)


def _apply_report_to_registry(
    registry: Registry,
    msg_type: int,
    fields: dict[str, object],
    tag: bytes | None,
    at: datetime,
) -> None:
    if msg_type == payloads.HEARTBEAT:
        state_value = fields["state"]
        assert isinstance(state_value, int)
        registry.heartbeat(
            node_id=_BEHAVIOUR_4_NODE,
            state=trap_state_from_wire(state_value),
            at=at,
            battery_mv=fields.get("battery_mv"),  # type: ignore[arg-type]
            targets_version=fields.get("targets_version"),  # type: ignore[arg-type]
        )
    elif msg_type == payloads.CAPTURED:
        state_after = fields["state_after"]
        assert isinstance(state_after, int) and tag is not None
        registry.captured(
            node_id=_BEHAVIOUR_4_NODE, tag=tag, state=trap_state_from_wire(state_after), at=at
        )
    else:  # FAULT
        # `_BEHAVIOUR_4_REPORT_SHAPES`'s own "fault" case fixes `code: 1`
        # (SERVO_STALLED) -- a generic fault, not one that names either
        # `command` parametrized above as refused
        # (`commands.FAULT_CODE_REFUSES` only maps 5,
        # ARM_REQUIRES_PRESENCE, to ARM), so this case is unaffected by
        # `fault()`'s new named-refusal cancellation and keeps proving the
        # *other* rule: a FAULT alone never satisfies a pending command.
        code = fields.get("code")
        registry.fault(_BEHAVIOUR_4_NODE, at=at, code=code if isinstance(code, int) else None)


def _behaviour_4_expected_pending(
    command: str, report_msg_type: int, report_fields: dict[str, object]
) -> str | None:
    """The rule this test proves, stated as code: a FAULT never satisfies a
    pending command (no state field of its own, S4 -- see `Registry.fault`'s
    own docstring); a HEARTBEAT or a CAPTURED's `state_after` does, exactly
    like `Command.satisfied_by` already defines for a HEARTBEAT.
    """
    if report_msg_type == payloads.FAULT:
        return command
    state_key = "state" if report_msg_type == payloads.HEARTBEAT else "state_after"
    armed = report_fields[state_key] == 1  # TrapState.ARMED's wire byte
    satisfied = armed if command == "arm" else not armed
    return None if satisfied else command


@pytest.mark.parametrize("command", ["arm", "disarm"])
@pytest.mark.parametrize(
    ("case_id", "report_msg_type", "report_fields", "report_tag"),
    _BEHAVIOUR_4_REPORT_SHAPES,
    ids=[shape[0] for shape in _BEHAVIOUR_4_REPORT_SHAPES],
)
def test_a_pending_command_agrees_live_vs_rehydrated_for_every_report_type(
    tmp_path: Path,
    command: str,
    case_id: str,
    report_msg_type: int,
    report_fields: dict[str, object],
    report_tag: bytes | None,
) -> None:
    """BEHAVIOUR 4 (verify-report-batch2d.md): an operator issues `command`,
    then the trap reports `report_msg_type` -- built two ways from the same
    two-event log (live, via direct `Registry` calls; rehydrated, via a real
    restart through `build_app`) and required to agree, and to agree with
    the rule `_behaviour_4_expected_pending` states explicitly. Before this
    fix: 5 of these 18 cases diverged (every `disarm` + non-`FAULT`,
    non-armed CAPTURED case, and `arm` + `captured_armed`) -- rehydration
    checked `satisfied_by` against a CAPTURED-won state race; the live path
    never did. `fault` must land on the *same* pending value on both sides
    now too (previously the mirror bug: a FAULT-won rehydration race used to
    also incorrectly satisfy a pending `disarm`).
    """
    now = datetime.now(UTC)
    now_epoch = now.timestamp()
    command_at = datetime.fromtimestamp(now_epoch - 60.0, tz=UTC)
    report_at = datetime.fromtimestamp(now_epoch - 30.0, tz=UTC)
    operator_msg_type = OPERATOR_ARM if command == "arm" else OPERATOR_DISARM

    db_path = tmp_path / "station.db"
    seed = Store(db_path)
    seed.append(
        StationEvent(
            source="operator",
            node_id=_BEHAVIOUR_4_NODE,
            msg_type=operator_msg_type,
            msg_id=None,
            node_epoch=None,
            tag=None,
            fields={},
            payload=None,
        ),
        received_epoch=now_epoch - 60.0,
        clock_trust="ntp",
    )
    seed.append(
        StationEvent(
            source="node",
            node_id=_BEHAVIOUR_4_NODE,
            msg_type=report_msg_type,
            msg_id=1,
            node_epoch=None,
            tag=report_tag,
            fields=report_fields,
            payload=b"",
        ),
        received_epoch=now_epoch - 30.0,
        clock_trust="ntp",
    )
    seed.close()

    live_registry = Registry(silence_after=timedelta(minutes=15))
    _apply_operator_command_to_registry(live_registry, command, at=command_at)
    _apply_report_to_registry(live_registry, report_msg_type, report_fields, report_tag, report_at)
    live_pending = live_registry.view(now=now)[0]["pending_command"]

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])
    with TestClient(app) as c:
        rehydrated_pending = {row["node_id"]: row for row in c.get("/api/traps").json()}[
            _BEHAVIOUR_4_NODE
        ]["pending_command"]

    expected = _behaviour_4_expected_pending(command, report_msg_type, report_fields)
    assert live_pending == expected, f"{case_id}/{command}: live pending={live_pending!r}"
    assert rehydrated_pending == expected, (
        f"{case_id}/{command}: rehydrated={rehydrated_pending!r} live={live_pending!r}"
    )


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


def test_boot_survives_a_broken_node_roster_query(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`_known_node_ids()`'s query and the loop over it used to sit above
    the per-step `try` entirely. Dropping the table is a deterministic
    stand-in for corruption landing on it.

    **Edited assertion (WARNING K(b), verify-report-batch1-round2.md).**
    Before this batch: asserted only `ok is True` and `rehydration_errors
    == 1` -- byte-for-byte the same response a single unreadable row among
    otherwise-healthy nodes produces (see
    `test_one_bad_row_does_not_look_like_a_lost_roster` below), even though
    this case is the whole registry coming up empty, every pending operator
    command gone until re-issued. That is the rule the report names: a
    failure that loses the whole registry is a degraded boot, not an error
    count, so it now gets its own field and folds into `ok` -- the same
    "fold connected into ok" precedent `link_ok` already sets, so a
    monitoring check or a person glancing at `ok` alone cannot read this as
    a healthy, merely quiet station.

    **Extended (WARNING 3, verify-report-batch2c.md): the fourth skip/drop
    log site, still unpinned.** `store.py` has three `logger.exception`
    sites, not two -- the state-race loop and `_rehydrate_desired_command`
    were each pinned by their own dedicated `caplog` test, but the roster
    failure at `store.py:556` (this very scenario) was measured only
    through `rehydration_errors`/`roster_lost`, never through its own log
    record. N7 (downgrading that site to `logger.debug`, dropping the
    traceback) survived all 757 tests. One `caplog` assertion here closes
    it, the same shape the other two sites already use.
    """
    db_path = tmp_path / "station.db"
    Store(db_path).close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])
    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("DROP TABLE node_cursor")
    raw.close()

    with (
        caplog.at_level(logging.DEBUG, logger="feeder_station.adapters.store"),
        TestClient(app) as c,
    ):
        response = c.get("/health")
        traps = c.get("/api/traps").json()

    assert response.status_code == 200
    body = response.json()
    assert body["rehydration_errors"] == 1
    assert body["roster_lost"] is True
    assert body["ok"] is False  # a lost registry must not read as a healthy station
    assert traps == []

    roster_records = [
        r for r in caplog.records if "could not read the node roster" in r.getMessage()
    ]
    assert len(roster_records) == 1
    record = roster_records[0]
    assert record.levelno == logging.ERROR  # not downgraded to DEBUG
    assert record.exc_info is not None  # the traceback -- not dropped


def test_one_bad_row_does_not_look_like_a_lost_roster(tmp_path: Path) -> None:
    """WARNING K(b) — the other half of the distinction: one unreadable
    row among otherwise-healthy nodes must keep reading as the ordinary,
    already-visible `rehydration_errors` count, not as a degraded boot.
    Three nodes seeded, one with an out-of-range `state` byte (CRITICAL
    2's own corruption technique); the other two are untouched.
    """
    db_path = tmp_path / "station.db"
    seed = Store(db_path)
    for node_id, state in ((1, 0), (2, 1)):
        seed.append(
            StationEvent(
                source="node",
                node_id=node_id,
                msg_type=payloads.HEARTBEAT,
                msg_id=1,
                node_epoch=None,
                tag=None,
                fields={"state": state, "battery_mv": 4000, "targets_version": 1},
                payload=b"",
            ),
            received_epoch=1000.0,
            clock_trust="unset",
        )
    seed.append(
        StationEvent(
            source="node",
            node_id=3,
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
    # An operator row too, so node 3's only *readable* row still creates it
    # in the registry (CRITICAL 2's own "no other readable row" case is a
    # separate scenario, already covered by
    # test_rehydrate_skips_and_counts_an_unreadable_row_without_blocking_other_nodes)
    # -- this test's own point is that three nodes, not zero, must still show.
    seed.append(
        StationEvent(
            source="operator",
            node_id=3,
            msg_type=OPERATOR_ARM,
            msg_id=None,
            node_epoch=None,
            tag=None,
            fields={},
            payload=None,
        ),
        received_epoch=1001.0,
        clock_trust="unset",
    )
    seed.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])

    with TestClient(app) as c:
        response = c.get("/health")
        traps = c.get("/api/traps").json()

    assert response.status_code == 200
    body = response.json()
    assert body["rehydration_errors"] == 1
    assert body["roster_lost"] is False
    assert body["ok"] is True  # one bad row among healthy nodes -- not a degraded boot
    assert len(traps) == 3  # the corrupt-state node is still present, WARNING J's own honesty


def test_a_partial_roster_says_so_instead_of_reading_healthy(tmp_path: Path) -> None:
    """WARNING 4 (verify-report-batch2.md): `roster_lost` is all-or-nothing
    -- it is set only when the roster *query itself* raises. A query that
    succeeds while returning fewer nodes than the store actually holds (a
    `node_cursor` row lost to a partial write, simulated here by deleting
    it directly while the node's own `HEARTBEAT` row survives in `event`)
    reads `ok: true`, `roster_lost: false`, `rehydration_errors: 0`, and
    silently drops the node from `/api/traps` -- exactly the case WARNING
    K(b) named as its own third, un-taken close: "a count of nodes
    expected vs rehydrated."
    """
    db_path = tmp_path / "station.db"
    seed = Store(db_path)
    for node_id in (1, 2, 3):
        seed.append(
            StationEvent(
                source="node",
                node_id=node_id,
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
    seed.close()

    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("DELETE FROM node_cursor WHERE node_id = 2")
    raw.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])
    with TestClient(app) as c:
        health = c.get("/health").json()
        traps = c.get("/api/traps").json()

    assert [row["node_id"] for row in traps] == [1, 3]  # node 2 silently dropped
    assert health["roster_lost"] is False  # the query itself did not raise
    assert health["rehydration_errors"] == 0  # no row anywhere was unreadable
    assert health["roster_nodes_expected"] == 3  # ground truth: `event` names 3 distinct nodes
    assert health["roster_nodes_rehydrated"] == 2  # `_known_node_ids()` found only 2 of them
    assert health["ok"] is False  # a roster that came back short must not read as healthy


def test_a_station_sourced_only_node_does_not_trip_the_partial_roster_check(
    tmp_path: Path,
) -> None:
    """WARNING 4 (verify-report-batch2c.md): `_known_node_ids()` and
    `_count_all_known_nodes()` used to differ *by design*, not only through
    corruption. `_count_all_known_nodes()` counts every distinct
    `node_id` in `event` regardless of `source`; `_known_node_ids()` only
    ever unioned `node_cursor` (node-sourced) with `operator`-sourced rows
    -- leaving out `source='station'` rows entirely
    (`adapters/clock.py::attempt_time_sync` writes a
    `STATION_TIME_SYNC_WITHHELD` row, naming a `node_id`, whenever S12
    refuses to distribute an unset clock). A node the station has only
    ever refused to time-sync -- never heard from, never commanded -- read
    `ok: false` permanently, with `roster_lost: false` and
    `rehydration_errors: 0` naming nothing: a by-design gap read as
    corruption. `_known_node_ids()` now unions station-sourced rows too.
    """
    db_path = tmp_path / "station.db"
    seed = Store(db_path)
    seed.append(
        StationEvent(
            source="node",
            node_id=1,
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
    seed.append(
        StationEvent(
            source="station",
            node_id=5,
            msg_type=STATION_TIME_SYNC_WITHHELD,
            msg_id=None,
            node_epoch=None,
            tag=None,
            fields={"reason": "clock_unset"},
            payload=None,
        ),
        received_epoch=1000.0,
        clock_trust="unset",
    )
    seed.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])
    with TestClient(app) as c:
        health = c.get("/health").json()

    # node 5's only row is station-sourced -- ground truth (`event`) and the
    # roster walk must agree that it is a real, known node, not a gap
    assert health["roster_nodes_expected"] == 2
    assert health["roster_nodes_rehydrated"] == 2
    assert health["ok"] is True  # a by-design source gap must not read as a lost roster


def test_state_may_be_stale_is_true_only_for_the_node_that_earned_it(tmp_path: Path) -> None:
    """WARNING 2 (verify-report-batch2.md): the only three assertions that
    ever read `state_may_be_stale` before this test live at the `Registry`
    unit level (`tests/adapters/test_store.py`), which calls
    `rehydrate()`/`restore_reported_state()` directly -- never through
    `Registry.view()`'s own serialization. Two mutations proved that gap:
    `view()` emitting `"state_may_be_stale": False` unconditionally, and
    the rehydration flag being set unconditionally on *every* trap, both
    survived the whole 748-test suite. A real `/api/traps` boot with one
    genuinely stale-flagged node and one genuinely healthy node in the same
    response kills both directions at once: node 7's newest FAULT row is
    unreadable (an older HEARTBEAT wins, flagged stale); node 9's only row
    is perfectly readable.
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
            fields={"state": 1, "battery_mv": 4000, "targets_version": 1},  # 1 -> ARMED
            payload=b"",
        ),
        received_epoch=1000.0,
        clock_trust="unset",
    )
    seed.append(
        StationEvent(
            source="node",
            node_id=7,
            msg_type=payloads.FAULT,
            msg_id=2,
            node_epoch=None,
            tag=None,
            fields={},
            payload=b"",
        ),
        received_epoch=1e20,  # unreadable: OverflowError inside _epoch_to_datetime
        clock_trust="unset",
    )
    seed.append(
        StationEvent(
            source="node",
            node_id=9,
            msg_type=payloads.HEARTBEAT,
            msg_id=1,
            node_epoch=None,
            tag=None,
            fields={"state": 0, "battery_mv": 4100, "targets_version": 1},  # 0 -> IDLE
            payload=b"",
        ),
        received_epoch=1000.0,
        clock_trust="unset",
    )
    seed.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])
    with TestClient(app) as c:
        rows = {row["node_id"]: row for row in c.get("/api/traps").json()}

    assert rows[7]["state_may_be_stale"] is True  # a newer FAULT existed and could not be read
    assert rows[9]["state_may_be_stale"] is False  # perfectly readable, nothing to doubt


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

    **Shifted assertion (WARNING 1, verify-report-batch2.md): both rows
    now count as unreadable, not only the heartbeat.** Before that fix, a
    `CAPTURED` row's missing `state_after` returned `None` silently --
    `_rehydrate_capture` never raised, so only the heartbeat's
    out-of-range `state` byte was counted. WARNING 1 closed exactly this
    gap: a row that cannot be read must be counted whether the code
    expresses that by raising or by returning `None`, so this node's
    `rehydration_errors` is now `2`, not `1` -- both of its two rows are
    genuinely unreadable, and the previous count under-reported that.
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
    assert health["rehydration_errors"] == 2  # WARNING 1: both rows are unreadable, not just one


# --- CRITICAL 6 (verify-report-2.md): a command to a node the station has
# never heard from must survive a restart -- `_known_node_ids()` derived the
# roster from `node_cursor`, which `Store.append` only ever populates for
# node-sourced events, so an operator-sourced row for a silent node was
# invisible to every rehydration walk. ---------------------------------------


def _issue_arm(c: TestClient, node_id: int) -> None:
    response = c.post(f"/api/traps/{node_id}/arm")
    assert response.status_code == 200


def _issue_disarm(c: TestClient, node_id: int) -> None:
    response = c.post(f"/api/traps/{node_id}/disarm")
    assert response.status_code == 200


def _issue_set_targets(c: TestClient, node_id: int) -> None:
    response = c.put(f"/api/traps/{node_id}/targets", json={"tags": ["0384199C87EC06"]})
    assert response.status_code == 200


def _issue_set_targets_then_disarm(c: TestClient, node_id: int) -> None:
    """CRITICAL 1 (verify-report-batch2c.md): the exact two-click sequence
    that reaches `_reported_state`'s `trap.state is None` branch with zero
    corruption. `PUT /targets` makes `restore_desired_tags` create the
    registry entry first (`store.py:782-786`), with `state` still at its
    own default, `None`; the `DISARM` that follows is then checked against
    that entry on the next boot -- the only path in this parametrize table
    that reaches the second `return None` branch in `_reported_state`
    rather than the first (`KeyError`, for a node with no entry at all).
    """
    _issue_set_targets(c, node_id)
    _issue_disarm(c, node_id)


# CRITICAL 1 (verify-report-batch2.md): the three operator-sourced msg_types
# `_known_node_ids()` unions in (`adapters/store.py::_OPERATOR_COMMAND_MSG_TYPES`)
# -- read from the code, not assumed -- so a roster narrowed to a single one of
# them cannot pass this parametrize table, only the un-parametrized single-case
# version the report measured.
#
# station/remote-arm-restored: ARM is parametrized here again.
# `POST /api/traps/{node_id}/arm` was gone for one commit (029b80c,
# "remove the remote arm affordance") and this case was dropped down to
# `test_a_legacy_arm_row_still_rehydrates_to_a_pending_command` below,
# which seeds an `OPERATOR_ARM` row directly against the store rather
# than through the (then-removed) HTTP route. The route exists again
# (the bench now runs the arming sweep on the node itself), so this table
# goes back to proving the real, full path -- operator presses Armar,
# the row is durable, a restart still shows it pending -- for ARM the
# same way it already does for DISARM and SET_TARGETS. The legacy test
# below stays: it is the only proof that a row seeded before this
# capability existed still rehydrates correctly, which no route call
# here would ever construct.
@pytest.mark.parametrize(
    ("issue_command", "expected_pending"),
    [
        pytest.param(_issue_arm, "arm", id="arm"),
        pytest.param(_issue_disarm, "disarm", id="disarm"),
        pytest.param(_issue_set_targets, "set_targets", id="set_targets"),
        # CRITICAL 1 (verify-report-batch2c.md): the previous three cases only
        # ever exercise `_reported_state`'s `KeyError` branch (no registry
        # entry at all) -- this one exercises the *other* branch (an entry
        # whose `state` is still `None`), which N2 proved unpinned: reverting
        # only that second branch to a fabricated `ReportedState(armed=False,
        # ...)` passed all 757 tests, because nothing reached it.
        pytest.param(_issue_set_targets_then_disarm, "disarm", id="set_targets_then_disarm"),
    ],
)
def test_a_command_to_a_never_heard_from_node_survives_a_restart(
    tmp_path: Path,
    issue_command: object,
    expected_pending: str,
) -> None:
    """The rule to prove: the roster walk includes every node with *any*
    row in the store, operator-sourced or node-sourced -- not "the
    report's own example survives". CRITICAL 1 (verify-report-batch2.md):
    an un-parametrized version of this test proved only the `ARM` example
    -- a roster narrowed to `source = 'operator' AND msg_type =
    OPERATOR_ARM` passed the whole 748-test suite. Parametrized over every
    operator route that can name a never-heard node, so a roster narrowed
    to any single one of them fails here.

    **Extended (CRITICAL 1, verify-report-batch2c.md): a fourth case,
    `set_targets_then_disarm`, proves `_reported_state`'s other `None`
    branch** -- the first three cases all reach a node with *no* registry
    entry at all (`_reported_state`'s `KeyError` branch); the fourth issues
    `PUT /targets` before `POST /disarm` against the same never-heard node,
    so the registry entry already exists (`state=None`) by the time
    `DISARM` is checked, reaching the second branch instead. Both branches
    of the two-branch rule `_reported_state`'s own docstring states are now
    covered by this one table, not only the branch the report's example
    happened to exercise.

    Two real boots against the same `station.db`, through `build_app` both
    times (the report's own boot harness, not a hand-called
    `rehydrate()`): boot 1 issues the command against a node this station
    has never received a frame from and confirms it pending; boot 2 must
    still show that node, still pending, and -- since it has never
    actually reported anything -- with `state: null`, never a fabricated
    `"idle"` (the same WARNING J discipline, on a node with zero readable
    rows rather than one unreadable one).

    `event-store/spec.md` "Registry rehydration at boot" ("without
    depending on the node re-sending anything"); `trap-commands/spec.md`
    "Target-list delivery with version reconciliation" (resend continues
    until the version is confirmed -- which cannot hold across a restart
    if the command itself is forgotten); S1, S2.
    """
    db_path = tmp_path / "station.db"
    never_heard_node = 99

    boot1, _args = build_app(["--db-path", str(db_path), "--no-gateway"])
    with TestClient(boot1) as c1:
        issue_command(c1, never_heard_node)  # type: ignore[operator]
        boot1_rows = {row["node_id"]: row for row in c1.get("/api/traps").json()}

    assert never_heard_node in boot1_rows, "not even boot 1 shows the command as pending"
    assert boot1_rows[never_heard_node]["pending_command"] == expected_pending

    boot2, _args2 = build_app(["--db-path", str(db_path), "--no-gateway"])
    with TestClient(boot2) as c2:
        boot2_rows = {row["node_id"]: row for row in c2.get("/api/traps").json()}

    assert never_heard_node in boot2_rows, (
        "LOST across the restart -- the roster query does not cover every operator-sourced msg_type"
    )
    assert boot2_rows[never_heard_node]["pending_command"] == expected_pending
    # never reported -- not a fabricated "idle" (WARNING J's own discipline)
    assert boot2_rows[never_heard_node]["state"] is None


def test_a_legacy_arm_row_still_rehydrates_to_a_pending_command(tmp_path: Path) -> None:
    """docs/pending.md, "Remove the remote arm affordance": `POST
    /api/traps/{node_id}/arm` is gone, but `Registry.command_arm` and
    `OPERATOR_ARM` are deliberately kept -- `adapters/store.py`'s
    rehydration (`_rehydrate_desired_command`) still has to make sense of
    an `OPERATOR_ARM` row already durable in a station's database from
    before this change. Seeded directly against the store, the way that
    row would actually exist -- never through the removed HTTP route --
    this proves the dependency is real, not merely asserted in a comment:
    a station booting against an old `station.db` still shows the node as
    pending `arm`, ready to be fault-cancelled by the node's next refusal
    (`Registry.fault`), rather than crashing or silently losing the row.
    """
    db_path = tmp_path / "station.db"
    never_heard_node = 99

    seed = Store(db_path)
    seed.append(
        StationEvent(
            source="operator",
            node_id=never_heard_node,
            msg_type=OPERATOR_ARM,
            msg_id=None,
            node_epoch=None,
            tag=None,
            fields={},
            payload=None,
        ),
        received_epoch=datetime(2026, 1, 1, tzinfo=UTC).timestamp(),
        clock_trust="unset",
    )
    seed.close()

    app, _args = build_app(["--db-path", str(db_path), "--no-gateway"])
    with TestClient(app) as c:
        rows = {row["node_id"]: row for row in c.get("/api/traps").json()}

    assert rows[never_heard_node]["pending_command"] == "arm"


def test_an_acknowledge_only_node_is_counted_in_the_roster_and_rehydrates_to_nothing(
    tmp_path: Path,
) -> None:
    """SUGGESTION 2 (verify-report-batch2c.md): `OPERATOR_CAPTURE_ACKNOWLEDGED`
    is the fourth operator `msg_type` (`domain/events.py:34-38`) --
    `_OPERATOR_COMMAND_MSG_TYPES` names only three. N14 (narrowing
    `_known_node_ids()`'s union to just those three) survives all 757
    tests: nothing in `_rehydrate_desired_command` or `_rehydrate_capture`
    reads an ACK row on its own, so an acknowledge-only node ends up with
    no `Trap` in the registry either way -- an equivalent mutant *today*.
    Pinned anyway, once, for symmetry: an equivalent mutant today is a real
    one the day something *does* read this row type.

    `POST /acknowledge` on a never-heard node 500s (SUGGESTION 8: durable
    row written by `_record_operator_event` before `registry.acknowledge`
    raises `KeyError` for a node with no entry) -- the row is durable
    regardless, so the roster still sees it.
    """
    db_path = tmp_path / "station.db"
    never_heard_node = 99

    boot1, _args = build_app(["--db-path", str(db_path), "--no-gateway"])
    with TestClient(boot1, raise_server_exceptions=False) as c1:
        response = c1.post(f"/api/traps/{never_heard_node}/acknowledge")
    assert response.status_code == 500  # SUGGESTION 8: durable row written, then a KeyError

    boot2, _args2 = build_app(["--db-path", str(db_path), "--no-gateway"])
    with TestClient(boot2) as c2:
        health = c2.get("/health").json()
        boot2_rows = {row["node_id"]: row for row in c2.get("/api/traps").json()}

    assert health["ok"] is True
    # the ACK row still names node 99, so the roster's ground truth and its
    # own walk agree -- N14's narrower union would agree too, which is
    # exactly why it is an equivalent mutant for this scenario
    assert health["roster_nodes_expected"] == health["roster_nodes_rehydrated"] == 1
    assert never_heard_node not in boot2_rows  # nothing reads an ACK row into a Trap


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


# --- CRITICAL 3 (sdd-verify field-station-p4, batch 3a): the station's own
# --- total transmit rate, measured for real off the wire -------------------


def test_station_total_frame_rate_holds_the_channel_budget_off_the_real_wire(
    tmp_path: Path,
) -> None:
    """3.1: the rule to prove is the station's *total* transmit rate --
    across GET_BACKLOG, command resends and TIME_SYNC -- staying under
    docs/protocol/messages.md's channel budget, measured off the wire
    through the real `cli.build_app` path, not computed from two
    constants. That is the exact failure mode verify-report-2.md named in
    the test this one supersedes
    (`test_default_backlog_pacing_holds_the_stations_own_airtime_budget`,
    rewritten -- not deleted -- in tests/adapters/test_workers.py, task
    3.6): "it measures nothing... is arithmetic asserting arithmetic."
    Real measured occupancy at the time of that report: 11.68%, against a
    ~1% budget.

    Ten traps, DISARMed through the real HTTP route and never confirming
    (nothing ever reports back a HEARTBEAT with `state` other than
    `armed`) -- the report's own worst case -- driven through a real pty,
    a real `SerialGateway`, and the real worker threads `with
    TestClient(app)` starts via the ASGI lifespan (the only way the real
    boot path runs at all, per this change's own CRITICAL 1 finding).
    DISARM stands in for the report's own original example, ARM: `POST
    /api/traps/{node_id}/arm` no longer exists (docs/pending.md, "Remove
    the remote arm affordance" -- arming needs a person at the trap, no
    downlink can do it), and DISARM is the same single-frame,
    never-confirms-without-a-heartbeat shape this measurement needs.

    `--channel-budget-frames-per-s` is set here, not left at the real
    default (~0.027/s): the *mechanism* under test -- the total across all
    three transmitters converges on the configured budget, not on any one
    driver's own pace -- does not depend on which rate is configured, and
    proving it against the unaccelerated default would need this test to
    run for minutes (`pacing.DEFAULT_CAPACITY` / rate). The real,
    unaccelerated default is measured separately, off this same real path,
    and its numbers are recorded in this batch's apply-progress rather
    than paid for on every `pytest` run.

    This sandbox's own clock resolves `UNSET` (no `/dev/rtc0`, no NTP) --
    the same S12 scenario messages.md itself describes as normal -- so
    TIME_SYNC is not expected to contribute a frame here; it is proven
    separately, under a controlled trusted clock, by
    `test_time_sync_alone_is_capped_by_the_shared_budget` in
    tests/adapters/test_workers.py.
    """
    db_path = tmp_path / "station.db"
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    os.close(slave_fd)
    channel_budget_frames_per_s = 3.0

    app, _args = build_app(
        [
            "--db-path",
            str(db_path),
            "--serial-device",
            slave_name,
            "--channel-budget-frames-per-s",
            str(channel_budget_frames_per_s),
        ]
    )

    try:
        with TestClient(app) as c:
            for node_id in range(1, 11):
                assert c.post(f"/api/traps/{node_id}/disarm").status_code == 200

            # The backoff burst allowed to settle, then a steady-state
            # window measured off the wire -- verify-report-2.md's own
            # methodology, reused deliberately rather than measuring from
            # t=0 (this fix's own bucket starts full, DEFAULT_CAPACITY, so
            # an initial burst is expected and allowed).
            settle_deadline = time.monotonic() + 1.5
            while time.monotonic() < settle_deadline:
                ready, _, _ = select.select([master_fd], [], [], 0.05)
                if ready:
                    os.read(master_fd, 4096)  # drain, discard

            wire = b""
            window_started = time.monotonic()
            deadline = window_started + 3.0
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master_fd], [], [], 0.05)
                if ready:
                    wire += os.read(master_fd, 4096)
            elapsed = time.monotonic() - window_started
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    blocks = [block for block in wire.split(bytes([0x00])) if block]
    decoded = [frames.decode(cobs_decode(block)) for block in blocks]
    frames_per_second = len(decoded) / elapsed
    airtime_occupancy = frames_per_second * 0.37  # docs/protocol/messages.md

    assert frames_per_second <= channel_budget_frames_per_s * 1.5, (
        f"got {frames_per_second:.2f} total frames/s off the real wire, "
        f"configured budget is {channel_budget_frames_per_s}/s "
        f"({airtime_occupancy:.1%} occupancy)"
    )
    # Real proof this is the station's *total*, not one driver: at least
    # two different categories contributed real frames on the real wire --
    # if only one had, this would prove nothing beyond what the test it
    # supersedes already claimed (falsely) to prove.
    observed_types = {f.msg_type for f in decoded}
    assert payloads.DISARM in observed_types
    assert payloads.GET_BACKLOG in observed_types


# --- BEHAVIOUR 1 (verify-report-final.md, sdd-verify field-station-p4): the
# --- same rule, proven through the real production wiring, not a double ---


def test_a_chunked_set_targets_send_charges_n_tokens_off_the_real_wire(
    tmp_path: Path,
) -> None:
    """The unit-level proof in tests/adapters/test_command_sender.py pins
    the charge `send_due_commands` asks the budget for; this pins the same
    fact through the real production wiring `cli.build_app` assembles --
    the real `SharedBudget`, the real scheduler thread, a real pty -- which
    PROOF 2 (verify-report-final.md) names as never exercised against a
    chunked send: the only prior split-list test uses `_AllowAllBudget()`.

    Two traps, each given the largest tag list `TargetsRequest` now
    accepts (81 tags, three `SET_TARGETS` chunks -- BEHAVIOUR 1's own
    bound, tied to `pacing.DEFAULT_CAPACITY`), submitted before either
    trap has ever been polled or synced. `rotation_index` starts at 0
    (`adapters/workers.py`) and only advances once the roster is non-empty,
    so the very first tick that sees these two traps always tries
    `commands` before `time_sync` or `poll` (`_ROTATIONS[0]`) -- not a
    race: the shared bucket still holds its full starting
    `DEFAULT_CAPACITY` (3.0) when that tick's chunked sends are evaluated.

    A correct charge (3 tokens for 3 chunks) lets exactly one trap's list
    through in that tick and leaves nothing for the other's; the pre-fix
    one-token charge lets *both* through in the same pass -- six frames
    spent from a three-token bucket. The budget is pinned explicitly to
    the LoRa constant here, not left at the real (now serial-cable, ~230/s)
    default (Defect 2, docs/pending.md): this test's own proof needs a
    refill slow enough that neither trap's second attempt, nor the second
    trap's first, can ever accumulate three fresh tokens inside its short
    observation window, which the fast cable default no longer guarantees
    -- the *rate* configured is not what BEHAVIOUR 1 is about, so pinning
    it to a named, slow constant keeps that arithmetic legible instead of
    inventing a new magic number.
    """
    db_path = tmp_path / "station.db"
    master_fd, slave_fd = pty.openpty()
    slave_name = os.ttyname(slave_fd)
    os.close(slave_fd)
    tags = [f"{i:014X}" for i in range(81)]  # BEHAVIOUR 1's own bound: 3 chunks, the max allowed

    app, _args = build_app(
        [
            "--db-path",
            str(db_path),
            "--serial-device",
            slave_name,
            "--channel-budget-frames-per-s",
            str(DEFAULT_CHANNEL_BUDGET_FRAMES_PER_S_LORA),
        ]
    )

    try:
        with TestClient(app) as c:
            assert c.put("/api/traps/1/targets", json={"tags": tags}).status_code == 200
            assert c.put("/api/traps/2/targets", json={"tags": tags}).status_code == 200

            wire = b""
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master_fd], [], [], 0.05)
                if ready:
                    wire += os.read(master_fd, 4096)
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    blocks = [block for block in wire.split(bytes([0x00])) if block]
    decoded = [frames.decode(cobs_decode(block)) for block in blocks]
    set_targets_frames = [f for f in decoded if f.msg_type == payloads.SET_TARGETS]

    assert len(set_targets_frames) == 3, (
        f"got {len(set_targets_frames)} SET_TARGETS frames off the real wire for two pending "
        "3-chunk sends drawn from a 3-token bucket -- must be exactly 3 (one trap's whole "
        "list), never 6 (both lists -- BEHAVIOUR 1's one-token-per-trap bypass)"
    )
