"""The specimen roster's HTTP face: ``GET``/``PATCH /api/specimens``.

station/specimen-roster (docs/pending.md, "The station has one kind of
storage and needs two"): an operator manages a roster and marks who is
wanted, instead of typing hex strings into each trap. Marking a specimen
"of interest" must reach every known node through the *existing*
``SET_TARGETS`` machinery -- the editor, the per-trap version
reconciliation and the resend already proven by
``tests/adapters/test_command_sender.py`` and this file's own
``PUT /api/traps/{id}/targets`` tests in ``test_app.py`` -- never a new,
parallel delivery path.

messages.md's own R3: "An edit that has not been delivered is visible in
the UI as a pending change, never silently assumed." Reusing
``Registry.command_set_targets``/``pending_command`` for this is what
makes that true here for free -- ``/api/traps`` already renders it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from feeder_station.adapters.store import Store
from feeder_station.app import create_app
from feeder_station.domain.traps import TrapState

NOW = datetime(2026, 1, 1, tzinfo=UTC)
TAG_A = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])
TAG_B = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0x00, 0x11])


def _store_app(tmp_path: Path) -> tuple[TestClient, Store]:
    store = Store(tmp_path / "station.db")
    app = create_app(store=store)
    return TestClient(app), store


def _seed_known_trap(client: TestClient, node_id: int) -> None:
    """A trap the registry already knows about, with no pending command --
    the same "the trap exists because it has contacted this station"
    precedent every other route in this project relies on, without
    exercising the wire codec at all.
    """
    app = client.app
    with app.state.registry_lock:  # type: ignore[attr-defined]
        app.state.registry.heartbeat(node_id, TrapState.IDLE, at=NOW)  # type: ignore[attr-defined]


def _seed_specimen(store: Store, tag: bytes) -> None:
    """A specimen the roster already knows -- via a real CAPTURED row, the
    same "the roster grows from the data" path a live station uses, never a
    manual insert.
    """
    from feeder_station.domain import payloads
    from feeder_station.domain.events import StationEvent

    store.append(
        StationEvent(
            source="node",
            node_id=1,
            msg_type=payloads.CAPTURED,
            msg_id=1,
            node_epoch=1000,
            tag=tag,
            fields={"state_after": 0},
            payload=b"",
        ),
        received_epoch=100.0,
        clock_trust="synced",
    )


# --- GET /api/specimens -------------------------------------------------------


def test_get_specimens_is_empty_with_no_store() -> None:
    client = TestClient(create_app())

    response = client.get("/api/specimens")

    assert response.status_code == 200
    body = response.json()
    assert body["specimens"] == []
    assert body["of_interest_count"] == 0
    assert body["of_interest_limit"] > 0


def test_get_specimens_lists_a_specimen_grown_from_a_read(tmp_path: Path) -> None:
    client, store = _store_app(tmp_path)
    _seed_specimen(store, TAG_A)

    body = client.get("/api/specimens").json()

    assert len(body["specimens"]) == 1
    row = body["specimens"][0]
    assert row["tag"] == TAG_A.hex().upper()
    assert row["of_interest"] is False
    assert row["species"] is None
    assert row["band_number"] is None
    assert row["times_seen"] == 1


# --- PATCH /api/specimens/{tag} -----------------------------------------------


def test_patch_specimen_updates_species_and_band_number_without_touching_targets(
    tmp_path: Path,
) -> None:
    client, store = _store_app(tmp_path)
    _seed_specimen(store, TAG_A)
    _seed_known_trap(client, 5)

    response = client.patch(
        f"/api/specimens/{TAG_A.hex()}",
        json={"species": "Zonotrichia capensis", "band_number": "AB123"},
    )

    assert response.status_code == 200
    row = client.get("/api/specimens").json()["specimens"][0]
    assert row["species"] == "Zonotrichia capensis"
    assert row["band_number"] == "AB123"

    # A species/band-number-only edit is not a targets edit -- no trap
    # should gain a pending SET_TARGETS from this alone.
    trap = client.get("/api/traps").json()[0]
    assert trap["pending_command"] is None


def test_patch_specimen_marking_of_interest_pushes_set_targets_to_every_known_trap(
    tmp_path: Path,
) -> None:
    client, store = _store_app(tmp_path)
    _seed_specimen(store, TAG_A)
    _seed_known_trap(client, 5)
    _seed_known_trap(client, 7)

    response = client.patch(f"/api/specimens/{TAG_A.hex()}", json={"of_interest": True})
    assert response.status_code == 200

    traps = {row["node_id"]: row for row in client.get("/api/traps").json()}
    for node_id in (5, 7):
        trap = traps[node_id]
        assert trap["pending_command"] == "set_targets"
        assert trap["desired_targets_version"] == 1
        assert trap["targets"] == [TAG_A.hex().upper()]


def test_each_traps_own_confirmation_is_independent(tmp_path: Path) -> None:
    """R3 -- messages.md: "the confirmation stays per trap": one shared
    list, but each trap's own ``targets_version`` counter, own pending
    command and own confirmation, computed from that trap's *own* history --
    never a version shared or counted across the roster.

    Trap 5 already has a confirmed edit at v1 (a prior, ordinary per-trap
    ``PUT /targets``) before the roster broadcast ever runs; trap 7 has
    none. The broadcast must bump each from *its own* current version --
    v2 for trap 5, v1 for trap 7 -- not the same number for both, and one
    trap confirming its own new version must never clear, or otherwise
    disturb, a sibling trap's own still-pending copy of the same edit.
    """
    client, store = _store_app(tmp_path)
    _seed_specimen(store, TAG_A)
    _seed_known_trap(client, 5)
    _seed_known_trap(client, 7)
    # Trap 5 already had one confirmed edit before the roster ever pushed
    # anything -- its own history, independent of trap 7's (which has none).
    client.put("/api/traps/5/targets", json={"tags": []})
    app = client.app
    with app.state.registry_lock:  # type: ignore[attr-defined]
        app.state.registry.heartbeat(  # type: ignore[attr-defined]
            5, TrapState.IDLE, at=NOW, targets_version=1
        )

    client.patch(f"/api/specimens/{TAG_A.hex()}", json={"of_interest": True})

    traps = {row["node_id"]: row for row in client.get("/api/traps").json()}
    assert traps[5]["desired_targets_version"] == 2  # from trap 5's own v1, not a shared counter
    assert traps[7]["desired_targets_version"] == 1  # trap 7 had no prior version of its own

    with app.state.registry_lock:  # type: ignore[attr-defined]
        app.state.registry.heartbeat(  # type: ignore[attr-defined]
            5, TrapState.IDLE, at=NOW, targets_version=2
        )

    traps = {row["node_id"]: row for row in client.get("/api/traps").json()}
    assert traps[5]["pending_command"] is None  # confirmed
    assert traps[7]["pending_command"] == "set_targets"  # still pending, untouched


def test_unmarking_of_interest_pushes_a_new_list_without_that_tag(tmp_path: Path) -> None:
    """docs/pending.md's "un-mark a specimen a node still holds": a node
    that already confirmed the old list must be told, not left believing a
    stale one -- never silently assumed corrected.
    """
    client, store = _store_app(tmp_path)
    _seed_specimen(store, TAG_A)
    _seed_known_trap(client, 5)
    client.patch(f"/api/specimens/{TAG_A.hex()}", json={"of_interest": True})
    app = client.app
    with app.state.registry_lock:  # type: ignore[attr-defined]
        app.state.registry.heartbeat(  # type: ignore[attr-defined]
            5, TrapState.IDLE, at=NOW, targets_version=1
        )
    assert client.get("/api/traps").json()[0]["pending_command"] is None  # confirmed, sanity check

    client.patch(f"/api/specimens/{TAG_A.hex()}", json={"of_interest": False})

    trap = client.get("/api/traps").json()[0]
    assert trap["pending_command"] == "set_targets"
    assert trap["desired_targets_version"] == 2
    assert trap["targets"] == []  # the shared list no longer includes TAG_A


def test_patch_specimen_unknown_tag_returns_404(tmp_path: Path) -> None:
    client, _store = _store_app(tmp_path)

    response = client.patch(f"/api/specimens/{TAG_A.hex()}", json={"of_interest": True})

    assert response.status_code == 404


def test_patch_specimen_with_no_store_returns_503() -> None:
    client = TestClient(create_app())

    response = client.patch(f"/api/specimens/{TAG_A.hex()}", json={"of_interest": True})

    assert response.status_code == 503


def test_patch_specimen_rejects_a_malformed_tag(tmp_path: Path) -> None:
    client, store = _store_app(tmp_path)
    _seed_specimen(store, TAG_A)

    response = client.patch("/api/specimens/not-hex", json={"of_interest": True})

    assert response.status_code == 422


def test_patch_specimen_rejects_marking_of_interest_past_the_shared_budget(tmp_path: Path) -> None:
    """pending.md: the roster model "dissolves the 81-tag-per-trap ceiling
    into a selection over a roster" -- dissolves, not removes: one shared
    ``SET_TARGETS`` still cannot carry more tags than the station's whole
    burst budget can ever send (`app.py::_MAX_TARGET_TAGS`), now spent on
    one list for the whole network instead of once per trap.
    """
    from feeder_station.app import _MAX_TARGET_TAGS

    client, store = _store_app(tmp_path)
    tags = [bytes([i, 0, 0, 0, 0, 0, 0]) for i in range(_MAX_TARGET_TAGS + 1)]
    for tag in tags:
        _seed_specimen_with_tag(store, tag)
    for tag in tags[:_MAX_TARGET_TAGS]:
        assert store.update_specimen(tag, {"of_interest": True})

    response = client.patch(f"/api/specimens/{tags[-1].hex()}", json={"of_interest": True})

    assert response.status_code == 422
    row = next(r for r in client.get("/api/specimens").json()["specimens"] if r["tag"] == tags[-1].hex().upper())
    assert row["of_interest"] is False


def _seed_specimen_with_tag(store: Store, tag: bytes) -> None:
    from feeder_station.domain import payloads
    from feeder_station.domain.events import StationEvent

    store.append(
        StationEvent(
            source="node",
            node_id=1,
            msg_type=payloads.CAPTURED,
            msg_id=int.from_bytes(tag[:2], "big") + 1,
            node_epoch=1000,
            tag=tag,
            fields={"state_after": 0},
            payload=b"",
        ),
        received_epoch=100.0,
        clock_trust="synced",
    )
