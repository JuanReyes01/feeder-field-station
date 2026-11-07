import io
from datetime import date, datetime

from openpyxl import Workbook

from feeder_station.adapters.specimen_import import read_specimens

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
    assert len(table.errors) == 2


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
