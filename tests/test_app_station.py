"""The station redesign: subjects in BioMark, interest per trap, lists that send
themselves, unknown readings, Excel import and reading export."""

import csv
import io
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from feeder_station import app as app_module
from feeder_station.adapters.clock import StationClock
from feeder_station.adapters.store import Store
from feeder_station.app import create_app, reconcile_targets
from feeder_station.domain import payloads
from feeder_station.domain.commands import CommandKind
from feeder_station.domain.events import StationEvent
from feeder_station.domain.traps import TrapState

NOW = datetime(2026, 1, 1, tzinfo=UTC)
TAG_A = bytes.fromhex("03843D3C797055")
TAG_B = bytes.fromhex("03843D3C2D199F")
MISREAD = bytes.fromhex("0384000000BEEF")
HEX_A = TAG_A.hex().upper()
HEX_B = TAG_B.hex().upper()
HEX_MISREAD = MISREAD.hex().upper()
BIOMARK_A = "900_263007596629"
CSV_LIST = (
    "PIT,Species,SpeciesAndSpecialCode,Sex,Age,BandNumber\n"
    f"{BIOMARK_A},Anthracothorax nigricollis,A. nigricollis,Female,Adult,C1\n"
    "900_263002593695,Colibri delphinae,,,,\n"
)


def _client(tmp_path: Path) -> tuple[TestClient, Store]:
    store = Store(tmp_path / "station.db")
    return TestClient(create_app(store=store)), store


def _heartbeat(client: TestClient, node_id: int) -> None:
    app = client.app
    with app.state.registry_lock:  # type: ignore[attr-defined]
        app.state.registry.heartbeat(  # type: ignore[attr-defined]
            node_id, TrapState.IDLE, at=datetime.now(UTC), targets_version=1
        )


def _read(store: Store, tag: bytes, epoch: int, node_id: int = 1, seq: int = 1) -> None:
    store.append(
        StationEvent(
            source="node",
            node_id=node_id,
            msg_type=payloads.READS,
            msg_id=seq,
            node_epoch=epoch,
            tag=tag,
            fields={"tag": tag.hex(), "epoch": epoch, "clock_trusted": True, "batch_count": 1},
            payload=b"",
        ),
        received_epoch=float(epoch),
        clock_trust="ntp",
    )


def _import(client: TestClient, content: str = CSV_LIST) -> dict[str, object]:
    response = client.post(
        "/api/specimens/import?filename=lista.csv", content=content.encode("utf-8")
    )
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


def _desired(client: TestClient, node_id: int) -> set[bytes]:
    trap = client.app.state.registry.trap(node_id)  # type: ignore[attr-defined]
    return set(trap.desired_tags or ())


def test_import_registers_subjects_in_biomark(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert _import(client)["created"] == 2
    rows = {r["biomark"]: r for r in client.get("/api/specimens").json()["specimens"]}
    assert rows[BIOMARK_A]["registered"] is True
    assert rows[BIOMARK_A]["species"] == "Anthracothorax nigricollis"
    assert rows[BIOMARK_A]["sex"] == "Female"


def test_importing_again_does_not_change_who_is_of_interest(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    client.put(f"/api/specimens/{HEX_A}/interest", json={"all_traps": True})
    assert _import(client)["updated"] == 2
    assert store.wanted_tags_for(1) == [TAG_A]


def test_a_read_of_an_unlisted_tag_is_kept_as_an_unknown_reading(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _read(store, MISREAD, int(datetime.now(UTC).timestamp()))
    [row] = client.get("/api/specimens").json()["specimens"]
    assert row["registered"] is False
    [visit] = client.get("/api/visits").json()["visits"]
    assert visit["label"] == "Lectura desconocida"
    assert visit["known"] is False


def test_a_read_of_a_listed_tag_shows_its_species(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    _read(store, TAG_A, int(datetime.now(UTC).timestamp()))
    [visit] = client.get("/api/visits").json()["visits"]
    assert visit["label"] == "Anthracothorax nigricollis"
    assert visit["biomark"] == BIOMARK_A


def test_interest_in_one_trap_only_reaches_that_trap(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _import(client)
    _heartbeat(client, 1)
    _heartbeat(client, 2)
    client.put(f"/api/specimens/{HEX_A}/interest", json={"node_ids": [1]})
    assert _desired(client, 1) == {TAG_A}
    assert _desired(client, 2) == set()


def test_interest_in_all_traps_reaches_a_trap_connected_later(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    _heartbeat(client, 1)
    client.put(f"/api/specimens/{HEX_A}/interest", json={"all_traps": True})
    assert store.wanted_tags_for(7) == [TAG_A]


def test_marking_many_at_once(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    _heartbeat(client, 1)
    response = client.post(
        "/api/specimens/interest", json={"tags": [HEX_A, HEX_B], "all_traps": True}
    )
    assert response.json() == {"updated": 2}
    assert set(store.wanted_tags_for(1)) == {TAG_A, TAG_B}
    client.post("/api/specimens/interest", json={"tags": [HEX_A, HEX_B], "all_traps": False})
    assert store.wanted_tags_for(1) == []


def test_a_stale_list_on_a_trap_is_replaced_on_its_own(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    registry = client.app.state.registry  # type: ignore[attr-defined]
    at = datetime.now(UTC)
    registry.heartbeat(1, TrapState.IDLE, at=at, targets_version=15)
    registry.restore_reported_targets(1, targets_version=15, tags=(MISREAD,))
    registry.heartbeat(1, TrapState.IDLE, at=at, targets_version=15)

    assert reconcile_targets(store, registry, StationClock(), at) == [1]
    assert registry.trap(1).pending_command.kind is CommandKind.SET_TARGETS
    assert registry.trap(1).desired_tags == ()
    # Waiting on its own confirmation, so a second pass cannot pile on.
    assert reconcile_targets(store, registry, StationClock(), at) == []


def test_a_trap_whose_list_is_right_is_left_alone(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    client.put(f"/api/specimens/{HEX_A}/interest", json={"all_traps": True})
    registry = client.app.state.registry  # type: ignore[attr-defined]
    at = datetime.now(UTC)
    registry.heartbeat(1, TrapState.IDLE, at=at, targets_version=3)
    registry.restore_reported_targets(1, targets_version=3, tags=(TAG_A,))
    registry.heartbeat(1, TrapState.IDLE, at=at, targets_version=3)
    assert reconcile_targets(store, registry, StationClock(), at) == []


def test_a_new_trap_shows_up_needing_preparation_and_keeps_its_profile(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _heartbeat(client, 2)
    [trap] = client.get("/api/overview").json()["traps"]
    assert (trap["name"], trap["has_latch"], trap["setup_done"]) == ("Trampa 0002", None, False)

    client.patch(
        "/api/traps/2/profile", json={"name": "Jardín", "has_latch": True, "setup_done": True}
    )
    [trap] = client.get("/api/overview").json()["traps"]
    assert (trap["name"], trap["has_latch"], trap["setup_done"]) == (
        "Trampa 0002 · Jardín",
        True,
        True,
    )
    assert (trap["code"], trap["place"]) == ("0002", "Jardín")


def test_a_trap_is_named_after_its_id_in_hex(tmp_path: Path) -> None:
    """The firmware takes the id from the board's MAC: 0x9C1E is "Trampa 9C1E"."""
    client, _ = _client(tmp_path)
    _heartbeat(client, 0x9C1E)
    [trap] = client.get("/api/overview").json()["traps"]
    assert (trap["name"], trap["code"]) == ("Trampa 9C1E", "9C1E")


def test_overview_reports_each_traps_own_clock_and_visits(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    epoch = int(datetime.now(UTC).timestamp())
    store.append(
        StationEvent(
            source="node",
            node_id=1,
            msg_type=payloads.HEARTBEAT,
            msg_id=9,
            node_epoch=epoch - 40,
            tag=None,
            fields={"state": 0, "clock_epoch": epoch - 40, "clock_source": 1},
            payload=b"",
        ),
        received_epoch=float(epoch),
        clock_trust="ntp",
    )
    _heartbeat(client, 1)
    _read(store, TAG_A, epoch, seq=10)
    overview = client.get("/api/overview").json()
    [trap] = overview["traps"]
    assert trap["clock_epoch"] == epoch - 40
    assert trap["clock_offset_s"] == -40
    assert trap["visits_today"] == 1
    assert overview["visits_today"] == 1


def test_export_reads_is_in_biomark_and_flags_unknown_readings(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    _read(store, TAG_A, 1793897307, seq=1)
    _read(store, MISREAD, 1793897309, seq=2)
    response = client.get("/api/export/reads.csv")
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert [r["biomark"] for r in rows] == [BIOMARK_A, "900_48879"]
    assert {r["trampa"] for r in rows} == {"0001"}
    assert rows[0]["especie"] == "Anthracothorax nigricollis"
    assert rows[0]["clasificacion"] == "registrado"
    assert rows[1]["clasificacion"] == "lectura desconocida"
    assert rows[1]["especie"] == ""


def test_a_capture_keeps_what_the_operator_found(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    _heartbeat(client, 1)
    store.append(
        StationEvent(
            source="node",
            node_id=1,
            msg_type=payloads.CAPTURED,
            msg_id=3,
            node_epoch=1793897307,
            tag=TAG_A,
            fields={"tag": TAG_A.hex(), "epoch": 1793897307, "state_after": 2},
            payload=b"",
        ),
        received_epoch=1793897307.0,
        clock_trust="ntp",
    )
    assert client.get("/api/captures").json()["captures"][0]["result"] is None
    client.post("/api/traps/1/acknowledge?result=sin_colibri")
    [capture] = client.get("/api/captures").json()["captures"]
    assert capture["result"] == "sin_colibri"
    assert capture["biomark"] == BIOMARK_A


def test_the_log_speaks_spanish_and_skips_heartbeats(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    _read(store, TAG_A, 1793897307)
    client.post("/api/traps/1/arm")
    texts = [e["text"] for e in client.get("/api/log").json()["events"]]
    assert texts == ["Armar: enviado", f"Lectura de Anthracothorax nigricollis ({BIOMARK_A})"]
    commands = client.get("/api/log?kind=commands").json()["events"]
    assert [e["kind"] for e in commands] == ["commands"]


def test_an_older_database_keeps_described_subjects_as_registered(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _read(store, MISREAD, 1793897307)
    client.patch(f"/api/specimens/{HEX_MISREAD}", json={"species": "Colibri"})
    [row] = client.get("/api/specimens").json()["specimens"]
    assert row["registered"] is True


def test_the_page_shows_biomark_and_never_loads_anything_external() -> None:
    html = (Path(app_module.__file__).parent / "static" / "index.html").read_text("utf-8")
    assert "Lectura desconocida" in html
    assert "Importar Excel" in html
    assert "/api/export/reads.csv" in html
    assert "s.biomark" in html
    assert "://" not in html


def test_adding_a_subject_can_mark_it_of_interest_everywhere(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _heartbeat(client, 1)
    created = client.post(
        "/api/specimens", json={"pit": BIOMARK_A, "species": "X", "of_interest": True}
    ).json()
    assert (created["registered"], created["all_traps"]) == (True, True)
    assert _desired(client, 1) == {TAG_A}
    assert store.wanted_tags_for(9) == [TAG_A]


def test_a_test_fire_disarms_then_re_arms_once_the_trap_is_released(tmp_path: Path) -> None:
    from feeder_station.app import finish_test_fires

    client, store = _client(tmp_path)
    registry = client.app.state.registry  # type: ignore[attr-defined]
    at = datetime.now(UTC)
    registry.heartbeat(1, TrapState.ARMED, at=at, targets_version=1)
    client.post("/api/traps/1/test-fire")
    assert registry.trap(1).pending_command.kind is CommandKind.DISARM
    # Still armed, DISARM still pending: nothing to do yet.
    assert finish_test_fires(store, registry, StationClock(), at) == []

    registry.heartbeat(1, TrapState.IDLE, at=at, targets_version=1)
    assert finish_test_fires(store, registry, StationClock(), at) == [1]
    assert registry.trap(1).pending_command.kind is CommandKind.ARM
    assert finish_test_fires(store, registry, StationClock(), at) == []
    texts = [e["text"] for e in client.get("/api/log?kind=commands").json()["events"]]
    assert texts[-1] == "Disparo de prueba: enviado"


def test_a_reset_while_arming_cancels_the_pending_arm(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    registry = client.app.state.registry  # type: ignore[attr-defined]
    at = datetime.now(UTC)
    registry.heartbeat(1, TrapState.IDLE, at=at, targets_version=1)
    client.post("/api/traps/1/arm")
    registry.fault(1, at=at, code=10)
    assert registry.trap(1).pending_command is None
