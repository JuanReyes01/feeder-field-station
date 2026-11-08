import io
from datetime import date, datetime, time

from openpyxl import Workbook

from feeder_station.adapters.specimen_import import Capture, read_specimens
from feeder_station.domain.tags import parse_tag

TAG = bytes.fromhex("03843D3C797055")
HEADER = "PIT,Species,SpeciesAndSpecialCode,Sex,Age,BandNumber"


def test_csv_with_the_cicg_columns() -> None:
    data = f"{HEADER}\n900_263007596629,Anthracothorax nigricollis,A. nigricollis,Female,Adult,NA\n"
    table = read_specimens("lista.csv", data.encode())
    assert table.errors == []
    assert table.specimens[TAG] == {
        "species": "Anthracothorax nigricollis",
        "species_code": "A. nigricollis",
        "sex": "Female",
        "age": "Adult",
    }


def test_xlsx_finds_the_sheet_with_the_pit_column() -> None:
    workbook = Workbook()
    workbook.active.append(["notas"])
    sheet = workbook.create_sheet("Lista")
    sheet.append(["PIT", "Species", "BandNumber"])
    sheet.append(["900_263007596629", "Colibri delphinae", 103])
    buffer = io.BytesIO()
    workbook.save(buffer)
    table = read_specimens("lista.xlsx", buffer.getvalue())
    assert table.specimens[TAG] == {"species": "Colibri delphinae", "band_number": "103"}


def test_repeated_captures_collapse_and_keep_the_newest_values() -> None:
    data = f"{HEADER}\n900_263007596629,X,,,,\n900_263007596629,,,Male,,\n"
    table = read_specimens("lista.csv", data.encode())
    assert table.specimens[TAG] == {"species": "X", "sex": "Male"}
    assert table.warnings == []


def test_a_tag_under_two_species_is_reported() -> None:
    data = f"{HEADER}\n900_263007596629,X,,,,\n900_263007596629,Y,,,,\n"
    assert len(read_specimens("lista.csv", data.encode()).warnings) == 1


def test_a_bad_row_is_reported_and_the_good_ones_still_load() -> None:
    data = f"{HEADER}\nbasura,X,,,,\n,Y,,,,\n900_263007596629,Z,,,,\n"
    table = read_specimens("lista.csv", data.encode())
    assert list(table.specimens) == [TAG]
    assert len(table.errors) == 1
    assert table.without_tag == 1


def test_a_file_without_pit_or_not_a_workbook_says_so() -> None:
    assert read_specimens("lista.csv", b"a,b\n1,2\n").errors == ["Falta la columna PIT."]
    assert read_specimens("lista.xlsx", b"no es excel").errors != []


def test_semicolons_from_a_spanish_excel_work() -> None:
    table = read_specimens("lista.csv", b"PIT;Species\n900_263007596629;X\n")
    assert table.specimens[TAG] == {"species": "X"}


DATED = "PIT,Species,DateOfPIT,DateOfPIT_MM-DD-YYYY\n"


def test_each_row_keeps_its_day_so_recaptures_have_several() -> None:
    data = (
        f"{DATED}900_263007596629,X,Oct-14-2013,10/14/2013\n900_263007596629,X,Feb-9-26,02/09/26\n"
    )
    table = read_specimens("lista.csv", data.encode())
    assert table.capture_days[TAG] == {date(2013, 10, 14), date(2026, 2, 9)}
    assert table.warnings == []


def test_the_month_day_year_column_is_the_fallback() -> None:
    data = f"{DATED}900_263007596629,X,NA,6/29/17\n"
    assert read_specimens("lista.csv", data.encode()).capture_days[TAG] == {date(2017, 6, 29)}


def test_a_spanish_fecha_column_is_day_first() -> None:
    data = "PIT,Fecha\n900_263007596629,9/2/2026\n900_263007596629,2026-03-20\n"
    days = read_specimens("lista.csv", data.encode()).capture_days[TAG]
    assert days == {date(2026, 2, 9), date(2026, 3, 20)}


def test_an_unreadable_date_is_reported_and_the_row_still_loads() -> None:
    data = f"{DATED}900_263007596629,X,ayer,13/45/2020\n"
    table = read_specimens("lista.csv", data.encode())
    assert TAG in table.specimens
    assert TAG not in table.capture_days
    assert len(table.warnings) == 1


def test_an_excel_date_cell_is_read_as_a_date() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["PIT", "DateOfPIT"])
    sheet.append(["900_263007596629", datetime(2026, 3, 19)])  # noqa: DTZ001 - Excel has no zone
    buffer = io.BytesIO()
    workbook.save(buffer)
    table = read_specimens("lista.xlsx", buffer.getvalue())
    assert table.capture_days[TAG] == {date(2026, 3, 19)}


DATABASE_HEADER = [
    "ID", "CICG_ID", "Localidad", "Especie", "Pit tag code", "Ring", "Age", "Sex",
    "Project", "Date", "Other code",
]  # fmt: skip
OLD_PIT = "900_263000799579"
NEW_PIT = "900_263000799580"


def _database(rows: list[list[object]]) -> bytes:
    """The shape of CICG_Individuals_database.xlsx: a sheet listing more PITs
    but no species first, then the captures with group titles above the header."""
    workbook = Workbook()
    decoy = workbook.active
    decoy.title = "Pittags"
    decoy.append(["Pit tag code", None])
    for n in range(10):
        decoy.append([f"900_26300000{n:04d}", None])
    sheet = workbook.create_sheet("Birds 2025")
    sheet.append(["CICG CODE", None, "IDENTIFICATION"])
    sheet.append(DATABASE_HEADER)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_the_individuals_database_is_read_from_its_capture_sheet() -> None:
    data = _database(
        [
            [1, "CICG_1", "CICG", "Colibri coruscans", "900_263007596629", "G00530",
             "Adult", "Male", "Pit tagging intensivo", datetime(2025, 9, 5), "-"],  # noqa: DTZ001
            # A recapture written without the PIT still counts for the bird's tag.
            [2, "CICG_1", "CICG-Zona alta", "Colibri coruscans", "-", "-",
             "Adult", "Unknown", "CICG", datetime(2026, 2, 10), None],  # noqa: DTZ001
            [3, "CICG_2"],  # pre-filled, no capture yet
            [4, "CICG_3", "CICG", "Amazilia tzacatl", "NA", "C00001",
             "Adult", "Female", "CICG", datetime(2025, 1, 1), None],  # noqa: DTZ001
        ]
    )  # fmt: skip
    table = read_specimens("CICG_Individuals_database.xlsx", data)
    assert table.sheet == "Birds 2025"
    assert table.errors == []
    assert table.specimens[TAG] == {
        "cicg_id": "CICG_1",
        "species": "Colibri coruscans",
        "band_number": "G00530",
        "age": "Adult",
        "sex": "Male",  # a later "Unknown" does not erase it
    }
    assert table.captures[TAG] == {
        date(2025, 9, 5): Capture(
            date(2025, 9, 5), "Pit tagging intensivo", "CICG", {"age": "Adult", "sex": "Male"}
        ),
        # Each capture keeps what was written that day, "Unknown" included.
        date(2026, 2, 10): Capture(
            date(2026, 2, 10), "CICG", "CICG-Zona alta", {"age": "Adult", "sex": "Unknown"}
        ),
    }
    assert table.without_tag == 2
    assert table.replaced == set()


def test_each_capture_keeps_its_whole_record() -> None:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["CICG CODE", None, "MEASURES"])
    sheet.append(
        [
            "CICG_ID", "Especie", "Pit tag code", "Date", "Time captured", "Measured by",
            "weight", "wing length", "Exposed culmen", "Primaries", "Comments molt",
            "Wing ph", "Tail ph", "Sample (Y/N)", "Comments",
        ]
    )  # fmt: skip
    sheet.append(
        [
            "CICG_1", "Colibri coruscans", "900_263007596629", datetime(2025, 9, 5),  # noqa: DTZ001
            time(9, 9), "MAMA", 7.1, "68,5", "NA", "X", "p10,8", True, False, "N", "ok",
        ]
    )  # fmt: skip
    buffer = io.BytesIO()
    workbook.save(buffer)
    table = read_specimens("CICG_Individuals_database.xlsx", buffer.getvalue())
    assert table.errors == []
    capture = table.captures[TAG][date(2025, 9, 5)]
    assert capture.details == {
        "time_captured": "09:09",
        "measured_by": "MAMA",
        "weight": 7.1,
        "wing": 68.5,  # a comma decimal typed as text
        "molt_primaries": "X",
        "molt_comments": "p10,8",
        "photo_wing": True,  # an unticked photo is left out
        "sample": "N",
        "comments": "ok",
    }


def test_a_list_without_codes_cannot_tell_a_lost_tag() -> None:
    data = f"{HEADER}\n900_263007596629,X,,,,\n"
    assert read_specimens("lista.csv", data.encode()).replaced is None


def test_a_tag_the_bird_lost_is_flagged() -> None:
    old, new = parse_tag(OLD_PIT), parse_tag(NEW_PIT)
    data = _database(
        [
            [1, "CICG_1", "CICG", "X", OLD_PIT, None, None, None, None, datetime(2025, 1, 1), None],  # noqa: DTZ001
            # The protocol: the new tag's row copies the lost one into Other code,
            # and writing its own PIT there means nothing.
            [2, "CICG_1", "CICG", "X", NEW_PIT, None, None, None, None, datetime(2025, 6, 1), f"{OLD_PIT}; {NEW_PIT}"],  # noqa: DTZ001
            # Without the note, a newer tag on the same bird says the same.
            [3, "CICG_9", "CICG", "Y", "900_263000799581", None, None, None, None, datetime(2024, 1, 1), None],  # noqa: DTZ001
            [4, "CICG_9", "CICG", "Y", "900_263000799582", None, None, None, None, datetime(2024, 5, 1), None],  # noqa: DTZ001
            # A PIT written as lost but read again afterwards was not lost.
            [5, "CICG_7", "CICG", "Z", "900_263000799583", None, None, None, None, datetime(2024, 1, 1), "900_263000799584"],  # noqa: DTZ001
            [6, "CICG_8", "CICG", "Z", "900_263000799584", None, None, None, None, datetime(2024, 3, 1), None],  # noqa: DTZ001
        ]
    )  # fmt: skip
    table = read_specimens("base.xlsx", data)
    assert table.replaced == {old, parse_tag("900_263000799581")}
    assert new not in table.replaced
