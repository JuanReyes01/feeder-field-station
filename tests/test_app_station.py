"""The station redesign: subjects in BioMark, interest per trap, lists that send
themselves, unknown readings, Excel import and reading export."""

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from feeder_station import app as app_module
from feeder_station.adapters.clock import StationClock
from feeder_station.adapters.serial_gateway import DeviceConfig, SerialGateway
from feeder_station.adapters.settings import load_settings
from feeder_station.adapters.store import Store
from feeder_station.app import (
    create_app,
    finish_test_fires,
    keep_traps_paused,
    reconcile_targets,
    renumber_unconfirmed_lists,
)
from feeder_station.domain import payloads
from feeder_station.domain.commands import Command, CommandKind
from feeder_station.domain.events import StationEvent
from feeder_station.domain.traps import Registry, TrapState

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
    assert [r["biomark"] for r in rows] == [BIOMARK_A, "900_000000048879"]
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


def test_answering_a_capture_tells_the_trap_to_forget_the_last_tag(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _heartbeat(client, 1)
    for result in ("capturado", "sin_colibri"):
        client.app.state.registry.trap(1).resolve_capture_sends_left = 0  # type: ignore[attr-defined]
        client.post(f"/api/traps/1/acknowledge?result={result}")
        [trap] = client.get("/api/traps").json()
        assert trap["resolve_capture_pending"] is True
        assert client.app.state.registry.trap(1).resolve_capture_sends_left == 2  # type: ignore[attr-defined]


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
    assert "Importar base del CICG" in html
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


# --- the gateway's port, chosen from the interface ---------------------------


@dataclass
class _Listed:
    device: str
    vid: int | None
    pid: int | None
    description: str = ""


def _gateway_client(tmp_path: Path) -> tuple[TestClient, SerialGateway]:
    ports = [
        _Listed("/dev/ttyUSB1", 0x10C4, 0xEA60, "CP2102"),
        _Listed("/dev/ttyUSB0", 0x10C4, 0xEA60),
    ]
    gateway = SerialGateway(DeviceConfig(auto=True), list_ports=lambda: ports)
    app = create_app(
        store=Store(tmp_path / "campo.db"),
        gateway=gateway,
        settings_path=tmp_path / "campo.settings.json",
    )
    return TestClient(app), gateway


def test_the_interface_lists_the_ports_the_gateway_could_be_on(tmp_path: Path) -> None:
    client, _ = _gateway_client(tmp_path)
    body = client.get("/api/gateway").json()
    assert body["enabled"] is True
    assert body["configured"] == "auto"
    assert [p["device"] for p in body["ports"]] == ["/dev/ttyUSB0", "/dev/ttyUSB1"]
    assert body["ports"][1]["description"] == "CP2102"


def test_choosing_a_port_applies_it_and_remembers_it(tmp_path: Path) -> None:
    client, gateway = _gateway_client(tmp_path)
    body = client.put("/api/gateway", json={"device": "/dev/ttyUSB1"}).json()

    assert body["configured"] == "/dev/ttyUSB1"
    assert gateway.config == DeviceConfig(explicit_path="/dev/ttyUSB1")
    saved = load_settings(tmp_path / "campo.settings.json")
    assert saved.serial_device == "/dev/ttyUSB1"

    client.put("/api/gateway", json={"device": "auto"})
    assert gateway.config == DeviceConfig(auto=True)


def test_an_empty_port_choice_is_refused(tmp_path: Path) -> None:
    client, _ = _gateway_client(tmp_path)
    assert client.put("/api/gateway", json={"device": "  "}).status_code == 422


def test_a_station_without_gateway_says_so(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.get("/api/gateway").json()["enabled"] is False
    assert client.put("/api/gateway", json={"device": "COM3"}).status_code == 409


def test_the_page_has_its_icon(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    response = client.get("/favicon.png")
    assert response.status_code == 200
    assert response.content[:4] == b"\x89PNG"
    assert 'href="/favicon.png"' in client.get("/").text


# --- the update notice -----------------------------------------------------


def test_a_source_run_has_no_update_notice(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert client.get("/api/update").json() == {"enabled": False, "available": False}
    assert client.post("/api/update").status_code == 409


def test_the_update_notice_and_button_go_through_the_checker(tmp_path: Path) -> None:
    from feeder_station.adapters.updates import INSTALLED, UpdateChecker, asset_name

    launched: list[Path] = []
    name = asset_name("0.3.0", "windows", INSTALLED)
    release = {
        "tag_name": "v0.3.0",
        "html_url": "https://github.com/x/releases/tag/v0.3.0",
        "assets": [{"name": name, "browser_download_url": f"https://dl/{name}"}],
    }
    checker = UpdateChecker(
        current="0.2.0",
        system="windows",
        mode=INSTALLED,
        fetch=lambda: [release],
        download=lambda _url, target: target.write_bytes(b"setup"),
        launch_installer=launched.append,
        reinstall=lambda _p: None,
        request_shutdown=lambda: None,
        background=False,
    )
    client = TestClient(create_app(store=Store(tmp_path / "campo.db"), updates=checker))

    status = client.get("/api/update").json()
    assert status["available"] is True
    assert status["latest"] == "0.3.0"
    assert client.post("/api/update").json()["state"] == "done"
    assert [p.name for p in launched] == [name]


def _change(client: TestClient, **body: object) -> dict[str, object]:
    response = client.post("/api/specimens/interest/change", json=body)
    assert response.status_code == 200, response.text
    result: dict[str, object] = response.json()
    return result


def test_import_keeps_each_subjects_days_and_another_list_adds_to_them(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    dated = f"PIT,Species,DateOfPIT\n{BIOMARK_A},X,Oct-14-2013\n{BIOMARK_A},X,Feb-9-2026\n"
    assert _import(client, dated)["dated"] == 1
    [row] = client.get("/api/specimens").json()["specimens"]
    assert row["capture_days"] == ["2013-10-14", "2026-02-09"]
    _import(client, f"PIT,Species,DateOfPIT\n{BIOMARK_A},X,Feb-10-2026\n")
    [row] = client.get("/api/specimens").json()["specimens"]
    assert row["capture_days"] == ["2013-10-14", "2026-02-09", "2026-02-10"]


def test_typed_biomarks_and_bands_are_marked_and_the_rest_reported(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    _heartbeat(client, 1)
    result = _change(client, action="add", entries=["c1", "900_263002593695", "Z99", " "])
    assert (result["changed"], result["missing"]) == (2, ["Z99"])
    assert set(store.wanted_tags_for(1)) == {TAG_A, TAG_B}
    assert _desired(client, 1) == {TAG_A, TAG_B}


def test_a_typed_pit_nobody_registered_is_registered_and_marked(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert _change(client, action="add", entries=[BIOMARK_A])["created"] == [BIOMARK_A]
    [row] = client.get("/api/specimens").json()["specimens"]
    assert (row["registered"], row["all_traps"]) == (True, True)


def test_removing_a_pit_nobody_registered_registers_nothing(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    assert _change(client, action="remove", entries=[BIOMARK_A])["missing"] == [BIOMARK_A]
    assert client.get("/api/specimens").json()["specimens"] == []


def test_adding_one_trap_keeps_the_others(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    _heartbeat(client, 1)
    _heartbeat(client, 2)
    _change(client, action="add", node_id=1, tags=[HEX_A])
    _change(client, action="add", node_id=2, tags=[HEX_A, HEX_B])
    assert store.wanted_tags_for(1) == [TAG_A]
    assert set(store.wanted_tags_for(2)) == {TAG_A, TAG_B}


def test_removing_one_trap_from_a_subject_wanted_everywhere(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    _heartbeat(client, 1)
    _heartbeat(client, 2)
    _change(client, action="add", tags=[HEX_A])
    assert _change(client, action="remove", node_id=1, tags=[HEX_A])["changed"] == 1
    assert store.wanted_tags_for(1) == []
    assert store.wanted_tags_for(2) == [TAG_A]
    assert _change(client, action="remove", node_id=1, tags=[HEX_A])["unchanged"] == 1


def test_an_unknown_reading_can_be_marked_in_one_step(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _read(store, MISREAD, int(datetime.now(UTC).timestamp()))
    _change(client, action="add", tags=[HEX_MISREAD])
    [row] = client.get("/api/specimens").json()["specimens"]
    assert (row["registered"], row["all_traps"]) == (True, True)


def test_a_band_two_subjects_share_is_not_guessed(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _import(client, "PIT,BandNumber\n900_263007596629,C1\n900_263002593695,C1\n")
    result = _change(client, action="add", entries=["C1"])
    assert (result["changed"], result["ambiguous"]) == (0, ["C1"])


def test_more_than_a_trap_holds_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _heartbeat(client, 1)
    limit = client.get("/api/specimens").json()["of_interest_limit"]
    pits = [f"{0x03843D00000000 + i:014X}" for i in range(limit + 1)]
    response = client.post(
        "/api/specimens/interest/change", json={"action": "add", "entries": pits}
    )
    assert response.status_code == 422
    assert "Trampa 0001" in response.json()["detail"]
    assert client.get("/api/specimens").json()["specimens"] == []


def _excel_day(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day)  # noqa: DTZ001 - an Excel date cell has no zone


def test_the_individuals_database_imports_with_codes_projects_and_lost_tags(
    tmp_path: Path,
) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Birds 2025"
    sheet.append(["CICG CODE", None, "IDENTIFICATION"])
    sheet.append(
        ["CICG_ID", "Especie", "Pit tag code", "Ring", "Project", "Localidad", "Date", "Other code"]
    )
    sheet.append(["CICG_1", "X", "900_263002593695", "G1", "CICG", "CICG", _excel_day(2025, 1, 1)])
    sheet.append(
        [
            "CICG_1",
            "X",
            BIOMARK_A,
            "G1",
            "Pit tagging intensivo",
            "CICG-Zona alta",
            _excel_day(2025, 6, 1),
            "900_263002593695",
        ]
    )
    buffer = io.BytesIO()
    workbook.save(buffer)
    client, store = _client(tmp_path)
    response = client.post("/api/specimens/import?filename=base.xlsx", content=buffer.getvalue())
    assert response.json()["sheet"] == "Birds 2025"
    assert response.json()["replaced"] == 1
    rows = {r["biomark"]: r for r in client.get("/api/specimens").json()["specimens"]}
    assert rows[BIOMARK_A]["cicg_id"] == "CICG_1"
    assert rows[BIOMARK_A]["captures"] == [
        {"day": "2025-06-01", "project": "Pit tagging intensivo", "locality": "CICG-Zona alta"}
    ]
    assert rows["900_263002593695"]["replaced"] is True
    # The short list cannot tell a lost tag, so it leaves the flag alone, and
    # its day adds to the database's without erasing project and locality.
    _import(client, "PIT,Species,DateOfPIT\n900_263002593695,X,Jan-1-2025\n")
    rows = {r["biomark"]: r for r in client.get("/api/specimens").json()["specimens"]}
    assert rows["900_263002593695"]["replaced"] is True
    assert rows["900_263002593695"]["captures"] == [
        {"day": "2025-01-01", "project": "CICG", "locality": "CICG"}
    ]
    # The bird's code means the tag it wears now.
    _heartbeat(client, 1)
    assert _change(client, action="add", entries=["cicg_1"])["changed"] == 1
    assert store.wanted_tags_for(1) == [TAG_A]


def _registry(client: TestClient) -> Registry:
    registry: Registry = client.app.state.registry  # type: ignore[attr-defined]
    return registry


def test_a_new_list_never_reuses_a_number_still_in_flight(tmp_path: Path) -> None:
    """A trap ignores a version it already started to receive, so reusing one
    while a list is in flight left the trap ignoring every list after it."""
    client, _ = _client(tmp_path)
    _import(client)
    _heartbeat(client, 1)  # the trap holds version 1
    _change(client, action="add", tags=[HEX_A])
    trap = _registry(client).trap(1)
    assert trap.pending_command is not None
    assert trap.pending_command.targets_version == 2
    _change(client, action="add", tags=[HEX_B])  # before the trap confirms 2
    assert trap.pending_command.targets_version == 3


def test_the_last_number_sent_survives_a_restart(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    _heartbeat(client, 1)
    _change(client, action="add", tags=[HEX_A])  # sent as 2, never confirmed
    registry = Registry(silence_after=timedelta(minutes=5))
    store.rehydrate(registry)
    assert registry.next_targets_version(1) == 3


def test_a_list_waits_for_a_pending_arm_instead_of_replacing_it(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    _heartbeat(client, 1)
    registry = _registry(client)
    client.post("/api/traps/1/arm")
    _change(client, action="add", tags=[HEX_A])
    trap = registry.trap(1)
    assert trap.pending_command is not None
    assert trap.pending_command.kind is CommandKind.ARM
    assert trap.desired_tags == (TAG_A,)
    # Once the trap confirms the arm and reports its list, the list goes out.
    at = datetime.now(UTC)
    registry.heartbeat(1, TrapState.ARMED, at=at, targets_version=1)
    registry.restore_reported_targets(1, targets_version=1, tags=())
    registry.heartbeat(1, TrapState.ARMED, at=at, targets_version=1)
    assert reconcile_targets(store, registry, StationClock(), at) == [1]
    assert trap.pending_command is not None
    assert trap.pending_command.kind is CommandKind.SET_TARGETS


def test_a_test_fire_is_not_left_released_by_a_list(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    _import(client)
    registry = _registry(client)
    at = datetime.now(UTC)
    registry.heartbeat(1, TrapState.ARMED, at=at, targets_version=1)
    client.post("/api/traps/1/test-fire")
    _change(client, action="add", tags=[HEX_A])  # waits: the test fire keeps the slot
    trap = registry.trap(1)
    assert trap.pending_command is not None
    assert trap.pending_command.kind is CommandKind.DISARM
    registry.heartbeat(1, TrapState.IDLE, at=at, targets_version=1)
    assert finish_test_fires(store, registry, StationClock(), at) == [1]
    assert trap.pending_command is not None
    assert trap.pending_command.kind is CommandKind.ARM


def test_a_list_already_in_flight_does_not_hold_a_test_fire_released(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    registry = _registry(client)
    at = datetime.now(UTC)
    registry.heartbeat(1, TrapState.IDLE, at=at, targets_version=1)
    registry.command_set_targets(1, targets_version=2, at=at, tags=[TAG_A])
    registry.trap(1).rearm_after_test_fire = True
    assert finish_test_fires(store, registry, StationClock(), at) == [1]
    pending = registry.trap(1).pending_command
    assert pending is not None
    assert pending.kind is CommandKind.ARM


def test_an_unconfirmed_list_gets_a_new_number_while_the_trap_keeps_reporting(
    tmp_path: Path,
) -> None:
    client, store = _client(tmp_path)
    _import(client)
    _heartbeat(client, 1)
    _change(client, action="add", tags=[HEX_A])  # version 2
    registry = _registry(client)
    trap = registry.trap(1)
    sent = datetime.now(UTC)
    for _ in range(3):
        assert trap.pending_command is not None
        trap.pending_command = trap.pending_command.sent(sent)
    soon = sent + timedelta(seconds=5)
    registry.heartbeat(1, TrapState.IDLE, at=soon, targets_version=1)
    # Three sends and a report, but only 5 s in: too soon for a new number.
    assert renumber_unconfirmed_lists(store, registry, StationClock(), soon) == []
    later = sent + timedelta(seconds=61)
    assert trap.pending_command is not None
    trap.pending_command = trap.pending_command.sent(later - timedelta(seconds=1))
    # Nothing heard since the last send: maybe out of range, so no new number.
    assert renumber_unconfirmed_lists(store, registry, StationClock(), later) == []
    # The trap reports, still on its old version: version 2 got dropped.
    registry.heartbeat(1, TrapState.IDLE, at=later, targets_version=1)
    assert renumber_unconfirmed_lists(store, registry, StationClock(), later) == [1]
    assert trap.pending_command is not None
    assert (trap.pending_command.targets_version, trap.pending_command.attempts) == (3, 0)
    latest = store.latest(1, app_module.OPERATOR_SET_TARGETS)
    assert latest is not None
    assert latest.fields["targets_version"] == 3


def test_a_list_is_resent_at_least_every_30_s() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    listed = Command(kind=CommandKind.SET_TARGETS, targets_version=2, attempts=9, last_sent_at=at)
    arm = Command(kind=CommandKind.ARM, attempts=9, last_sent_at=at)
    later = at + timedelta(seconds=31)
    assert listed.due_for_resend(later)
    assert not arm.due_for_resend(later)


def test_deactivating_all_disarms_every_trap_that_can_capture(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    registry = _registry(client)
    at = datetime.now(UTC)
    for node_id in (1, 2, 3):
        registry.heartbeat(node_id, TrapState.ARMED, at=at, targets_version=1)
    client.patch("/api/traps/3/profile", json={"has_latch": False})  # reader only
    assert client.post("/api/traps/pause").json()["node_ids"] == [1, 2]
    pending = registry.trap(1).pending_command
    assert pending is not None
    assert pending.kind is CommandKind.DISARM
    assert registry.trap(3).pending_command is None
    assert client.get("/api/overview").json()["paused"] is True
    # It is the station's own state: a restart keeps it.
    assert TestClient(create_app(store=store)).get("/api/overview").json()["paused"] is True


def test_a_trap_that_arms_during_the_pause_is_disarmed_again(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    registry = _registry(client)
    at = datetime.now(UTC)
    registry.heartbeat(1, TrapState.ARMED, at=at, targets_version=1)
    client.post("/api/traps/pause")
    registry.heartbeat(1, TrapState.IDLE, at=at, targets_version=1)  # confirmed released
    assert keep_traps_paused(store, registry, StationClock(), at) == []
    registry.heartbeat(1, TrapState.ARMED, at=at, targets_version=1)  # rebooted and armed itself
    registry.heartbeat(2, TrapState.ARMED, at=at, targets_version=1)  # connected meanwhile
    assert keep_traps_paused(store, registry, StationClock(), at) == [1, 2]


def test_activating_all_arms_them_and_ends_the_pause(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    registry = _registry(client)
    at = datetime.now(UTC)
    registry.heartbeat(1, TrapState.ARMED, at=at, targets_version=1)
    client.post("/api/traps/pause")
    registry.heartbeat(1, TrapState.IDLE, at=at, targets_version=1)
    assert client.post("/api/traps/resume").json() == {"paused": False, "node_ids": [1]}
    pending = registry.trap(1).pending_command
    assert pending is not None
    assert pending.kind is CommandKind.ARM
    assert keep_traps_paused(store, registry, StationClock(), at) == []
    assert client.get("/api/overview").json()["paused"] is False


def test_one_trap_cannot_be_armed_or_test_fired_while_all_are_paused(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    _registry(client).heartbeat(1, TrapState.ARMED, at=datetime.now(UTC), targets_version=1)
    client.post("/api/traps/pause")
    assert client.post("/api/traps/1/arm").status_code == 409
    assert client.post("/api/traps/1/test-fire").status_code == 409


def test_a_test_fire_in_progress_is_not_rearmed_after_a_pause(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    registry = _registry(client)
    at = datetime.now(UTC)
    registry.heartbeat(1, TrapState.ARMED, at=at, targets_version=1)
    client.post("/api/traps/1/test-fire")
    client.post("/api/traps/pause")
    registry.heartbeat(1, TrapState.IDLE, at=at, targets_version=1)
    assert finish_test_fires(store, registry, StationClock(), at) == []
    assert registry.trap(1).rearm_after_test_fire is False
