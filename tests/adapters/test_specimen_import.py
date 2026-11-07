import io

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
