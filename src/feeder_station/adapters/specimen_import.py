"""Read the CICG's list of tagged individuals from an Excel or CSV file.

The real list has one row per capture event, so a tag can repeat; the newest
non-empty value of each field wins, and a tag listed under two species is
reported rather than silently resolved. The ``PIT`` column holds BioMark.

Every row's date is kept, so a subject recaptured on three days has three:
those are the days the operator filters by. ``DateOfPIT`` (``Feb-9-2026``) is
read first and ``DateOfPIT_MM-DD-YYYY`` (``2/9/2026`` or ``02/09/26``) is the
fallback; a two-digit year means 20YY.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from openpyxl import load_workbook

from feeder_station.domain.tags import parse_tag

_NULL_TOKENS = {"", "null", "none", "na", "n/a"}
_TAG_COLUMNS = ("pit", "biomark", "tag", "rfid_tag")
_FIELD_COLUMNS = {
    "species": ("species", "especie"),
    "species_code": ("speciesandspecialcode", "codigo", "código"),
    "sex": ("sex", "sexo"),
    "age": ("age", "edad"),
    "band_number": ("bandnumber", "band_number", "anillo"),
}

# Month names as the CICG writes them (English), plus Spanish for a list typed
# here. Read by hand so the result never depends on the computer's locale.
_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("jan", "ene"),
            ("feb",),
            ("mar",),
            ("apr", "abr"),
            ("may",),
            ("jun",),
            ("jul",),
            ("aug", "ago"),
            ("sep", "set"),
            ("oct",),
            ("nov",),
            ("dec", "dic"),
        ),
        start=1,
    )
    for name in names
}
_NAMED_MONTH = re.compile(r"([A-Za-z]{3})[A-Za-z]*[-/ ](\d{1,2})[-/ ](\d{2}|\d{4})")
_SLASHED = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})")
_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


def _year(text: str) -> int:
    return 2000 + int(text) if len(text) == 2 else int(text)


def _day(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _common_date(value: object) -> date | None:
    """A spreadsheet date cell or an ISO date, whatever the column."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    match = _ISO.fullmatch(str(value).strip().split(" ")[0])
    return _day(int(match[1]), int(match[2]), int(match[3])) if match else None


def _named_month_date(value: object) -> date | None:
    found = _common_date(value)
    if found is not None:
        return found
    match = _NAMED_MONTH.fullmatch(str(value).strip())
    if match is None or match[1].lower() not in _MONTHS:
        return None
    return _day(_year(match[3]), _MONTHS[match[1].lower()], int(match[2]))


def _slashed_date(day_first: bool) -> Callable[[object], date | None]:
    def parse(value: object) -> date | None:
        found = _common_date(value)
        if found is not None:
            return found
        match = _SLASHED.fullmatch(str(value).strip())
        if match is None:
            return None
        first, second = int(match[1]), int(match[2])
        month, day = (second, first) if day_first else (first, second)
        return _day(_year(match[3]), month, day)

    return parse


# In priority order: the first column with a readable date wins for a row.
_DATE_COLUMNS: tuple[tuple[tuple[str, ...], Callable[[object], date | None]], ...] = (
    (("dateofpit",), _named_month_date),
    (("dateofpit_mm-dd-yyyy", "date"), _slashed_date(day_first=False)),
    (("fecha", "fecha_captura", "fecha de captura"), _slashed_date(day_first=True)),
)


@dataclass
class ImportedTable:
    specimens: dict[bytes, dict[str, str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Every day each subject appears on in the list (marking and recaptures).
    capture_days: dict[bytes, set[date]] = field(default_factory=dict)


def _clean(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return None if text.lower() in _NULL_TOKENS else text


def _read_rows(filename: str, data: bytes) -> list[list[object]]:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets:
                rows: list[list[object]] = [list(r) for r in sheet.iter_rows(values_only=True)]
                if rows and _column_of(rows[0], _TAG_COLUMNS) is not None:
                    return rows
            return []
        finally:
            workbook.close()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("latin-1")
    first_line = text.split("\n", 1)[0]
    delimiter = ";" if first_line.count(";") > first_line.count(",") else ","
    return [list(row) for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _column_of(header: list[object], names: tuple[str, ...]) -> int | None:
    normalized = [str(cell).strip().lower() if cell is not None else "" for cell in header]
    for name in names:
        if name in normalized:
            return normalized.index(name)
    return None


def read_specimens(filename: str, data: bytes) -> ImportedTable:
    table = ImportedTable()
    try:
        rows = _read_rows(filename, data)
    except Exception:  # noqa: BLE001 - a broken file can fail in any parser; the operator needs one answer
        table.errors.append("No se pudo leer el archivo. Usa un .xlsx o un .csv.")
        return table
    if not rows:
        table.errors.append("El archivo está vacío.")
        return table

    tag_column = _column_of(rows[0], _TAG_COLUMNS)
    if tag_column is None:
        table.errors.append("Falta la columna PIT.")
        return table
    columns = {name: _column_of(rows[0], aliases) for name, aliases in _FIELD_COLUMNS.items()}
    date_columns = [
        (column, parse)
        for aliases, parse in _DATE_COLUMNS
        if (column := _column_of(rows[0], aliases)) is not None
    ]

    for line, row in enumerate(rows[1:], start=2):
        raw_tag = _clean(row[tag_column]) if tag_column < len(row) else None
        if raw_tag is None:
            if any(_clean(cell) is not None for cell in row):
                table.errors.append(f"Fila {line}: no tiene PIT.")
            continue
        tag = parse_tag(raw_tag)
        if tag is None:
            table.errors.append(f"Fila {line}: PIT no válido ({raw_tag}).")
            continue
        specimen = table.specimens.setdefault(tag, {})
        for name, column in columns.items():
            if column is None or column >= len(row):
                continue
            value = _clean(row[column])
            if value is None:
                continue
            if name == "species" and specimen.get(name) not in (None, value):
                table.warnings.append(
                    f"Fila {line}: {raw_tag} aparece como {specimen[name]} y como {value}."
                )
            specimen[name] = value
        _read_date(table, tag, row, line, date_columns)
    return table


def _read_date(
    table: ImportedTable,
    tag: bytes,
    row: list[object],
    line: int,
    date_columns: list[tuple[int, Callable[[object], date | None]]],
) -> None:
    unreadable: str | None = None
    for column, parse in date_columns:
        if column >= len(row):
            continue
        raw = row[column]
        if isinstance(raw, date):
            value: object = raw
        else:
            text = _clean(raw)
            if text is None:
                continue
            value = text
        day = parse(value)
        if day is not None:
            table.capture_days.setdefault(tag, set()).add(day)
            return
        unreadable = unreadable or str(value)
    if unreadable is not None:
        table.warnings.append(f"Fila {line}: no se entiende la fecha ({unreadable}).")
