"""Read the CICG's tagged individuals from an Excel or CSV file.

Two shapes are read the same way:

- the short list (``PIT tagged individuals list.csv``): one row per tagging,
  columns ``PIT``, ``Species``, ``BandNumber``, ``DateOfPIT``...
- the individuals database (``CICG_Individuals_database.xlsx``): one row per
  capture, the header on the second row under group titles, columns
  ``CICG_ID``, ``Especie``, ``Pit tag code``, ``Ring``, ``Age``, ``Sex``,
  ``Project``, ``Date``, ``Localidad``, ``Other code``.

In a workbook, the sheet with a species column and the most readable PITs wins:
the database also holds lookup, pivot and analysis sheets. The header is looked
for in the first rows.

A tag repeats once per recapture. The newest non-empty value of each field wins,
except that "Unknown" never replaces a known sex or age, and a tag listed under
two species or two CICG codes is reported rather than silently resolved. Every
capture keeps its day, project and locality: those are what the operator filters
by. A recapture written with the CICG code but without the PIT still counts for
that bird's tag. ``DateOfPIT`` (``Feb-9-2026``) is read first and
``DateOfPIT_MM-DD-YYYY`` or ``Date`` (``2/9/2026``, or a date cell) is the
fallback; a two-digit year means 20YY.

A tag is replaced when the bird lost it and got another one. The CICG's protocol
copies the lost PIT into ``Other code`` of the capture that gave the new one, so
a PIT written there on or after its own last capture, by a row with another PIT
or none, is lost; and when one CICG code shows a new PIT first captured after an
old one's last capture, the old one is lost too. Either way the old tag is kept,
flagged, so a filter does not spend a trap's slots on a tag nobody wears.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import NamedTuple

from openpyxl import load_workbook

from feeder_station.domain.tags import parse_tag

_NULL_TOKENS = {"", "null", "none", "na", "n/a", "-", "_", "no info"}
_UNKNOWN = "unknown"
_TAG_COLUMNS = (
    "pit",
    "biomark",
    "tag",
    "rfid_tag",
    "pit tag code",
    "pit tag",
    "pit_tag",
    "pit code",
)
_SPECIES_COLUMNS = ("species", "especie")
_FIELD_COLUMNS = {
    "species": _SPECIES_COLUMNS,
    "species_code": ("speciesandspecialcode", "codigo", "código"),
    "sex": ("sex", "sexo"),
    "age": ("age", "edad"),
    "band_number": ("bandnumber", "band_number", "anillo", "ring"),
    "cicg_id": ("cicg_id", "cicg id", "código cicg", "codigo cicg"),
}
_CAPTURE_COLUMNS = {
    "project": ("project", "proyecto"),
    "locality": ("localidad", "locality", "location"),
}
_OTHER_CODE_COLUMNS = ("other code", "other codes", "otro código", "otros códigos")
_HEADER_SEARCH_ROWS = 10
_PIT_IN_TEXT = re.compile(r"\d{3}_\d{10,13}")

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


class Capture(NamedTuple):
    day: date
    project: str | None = None
    locality: str | None = None


@dataclass
class ImportedTable:
    specimens: dict[bytes, dict[str, str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # One capture per day per subject: the tagging and every recapture.
    captures: dict[bytes, dict[date, Capture]] = field(default_factory=dict)
    # Tags the bird no longer wears (see the module docstring); None when the
    # file has neither CICG codes nor Other code, so it cannot tell.
    replaced: set[bytes] | None = None
    sheet: str | None = None
    # Rows describing a bird with no PIT: no antenna can read it.
    without_tag: int = 0

    @property
    def capture_days(self) -> dict[bytes, set[date]]:
        return {tag: set(days) for tag, days in self.captures.items()}

    def add_capture(self, tag: bytes, capture: Capture) -> None:
        days = self.captures.setdefault(tag, {})
        known = days.get(capture.day)
        if known is None:
            days[capture.day] = capture
        else:
            days[capture.day] = Capture(
                capture.day,
                known.project or capture.project,
                known.locality or capture.locality,
            )


def _clean(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).strip()
    return None if text.lower() in _NULL_TOKENS else text


def _column_of(header: list[object], names: tuple[str, ...]) -> int | None:
    normalized = [str(cell).strip().lower() if cell is not None else "" for cell in header]
    for name in names:
        if name in normalized:
            return normalized.index(name)
    return None


def _header_index(rows: list[list[object]]) -> int | None:
    for index, row in enumerate(rows[:_HEADER_SEARCH_ROWS]):
        if _column_of(row, _TAG_COLUMNS) is not None:
            return index
    return None


def _readable_tags(rows: list[list[object]]) -> int:
    column = _column_of(rows[0], _TAG_COLUMNS)
    assert column is not None
    count = 0
    for row in rows[1:]:
        text = _clean(row[column]) if column < len(row) else None
        count += text is not None and parse_tag(text) is not None
    return count


@dataclass
class _Table:
    rows: list[list[object]]  # header first
    header_line: int  # the header's row number in the file, from 1
    sheet: str | None = None


def _read_table(filename: str, data: bytes) -> _Table | None:
    if filename.lower().endswith((".xlsx", ".xlsm")):
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            best: tuple[tuple[bool, int], _Table] | None = None
            for sheet in workbook.worksheets:
                # A look at the top first: only sheets with a tag column are read whole.
                top: list[list[object]] = [
                    list(r) for r in sheet.iter_rows(max_row=_HEADER_SEARCH_ROWS, values_only=True)
                ]
                start = _header_index(top)
                if start is None:
                    continue
                whole: list[list[object]] = [list(r) for r in sheet.iter_rows(values_only=True)]
                rows = whole[start:]
                score = (_column_of(rows[0], _SPECIES_COLUMNS) is not None, _readable_tags(rows))
                if best is None or score > best[0]:
                    best = (score, _Table(rows, start + 1, sheet.title))
            return best[1] if best else None
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
    lines: list[list[object]] = [
        list(row) for row in csv.reader(io.StringIO(text), delimiter=delimiter)
    ]
    first = _header_index(lines)
    return None if first is None else _Table(lines[first:], first + 1)


def _cell(row: list[object], column: int | None) -> str | None:
    return _clean(row[column]) if column is not None and column < len(row) else None


def read_specimens(filename: str, data: bytes) -> ImportedTable:
    table = ImportedTable()
    try:
        found = _read_table(filename, data)
    except Exception:  # noqa: BLE001 - a broken file can fail in any parser; the operator needs one answer
        table.errors.append("No se pudo leer el archivo. Usa un .xlsx o un .csv.")
        return table
    if found is None:
        table.errors.append("Falta la columna PIT.")
        return table
    table.sheet = found.sheet
    header = found.rows[0]
    tag_column = _column_of(header, _TAG_COLUMNS)
    assert tag_column is not None
    columns = {name: _column_of(header, aliases) for name, aliases in _FIELD_COLUMNS.items()}
    capture_columns = {
        name: _column_of(header, aliases) for name, aliases in _CAPTURE_COLUMNS.items()
    }
    other_code_column = _column_of(header, _OTHER_CODE_COLUMNS)
    date_columns = [
        (column, parse)
        for aliases, parse in _DATE_COLUMNS
        if (column := _column_of(header, aliases)) is not None
    ]

    tags_by_code: dict[str, set[bytes]] = {}
    untagged_recaptures: list[tuple[str, Capture]] = []
    # A PIT written in Other code by a row with another PIT or none, and that row's day.
    mentions: list[tuple[bytes, date | None]] = []
    for line, row in enumerate(found.rows[1:], start=found.header_line + 1):
        if not any(_clean(cell) is not None for cell in row):
            continue
        day = _row_date(table, row, line, date_columns)
        capture = (
            Capture(
                day, _cell(row, capture_columns["project"]), _cell(row, capture_columns["locality"])
            )
            if day is not None
            else None
        )
        code = _cell(row, columns["cicg_id"])
        raw_tag = _cell(row, tag_column)
        own = parse_tag(raw_tag) if raw_tag is not None else None
        for written in _PIT_IN_TEXT.findall(str(_cell(row, other_code_column) or "")):
            if (mentioned := parse_tag(written)) is not None and mentioned != own:
                mentions.append((mentioned, day))
        if raw_tag is None:
            # A pre-filled row with only its number is no capture yet.
            if day is not None or _cell(row, columns["species"]) is not None:
                table.without_tag += 1
            if code is not None and capture is not None:
                untagged_recaptures.append((code, capture))
            continue
        tag = own
        if tag is None:
            table.errors.append(f"Fila {line}: PIT no válido ({raw_tag}).")
            continue
        specimen = table.specimens.setdefault(tag, {})
        for name, column in columns.items():
            value = _cell(row, column)
            if value is None:
                continue
            if name in ("sex", "age") and value.lower() == _UNKNOWN and specimen.get(name):
                continue
            if name in ("species", "cicg_id") and specimen.get(name) not in (None, value):
                table.warnings.append(
                    f"Fila {line}: {raw_tag} aparece como {specimen[name]} y como {value}."
                )
            specimen[name] = value
        if code is not None:
            tags_by_code.setdefault(code, set()).add(tag)
        if capture is not None:
            table.add_capture(tag, capture)

    for code, capture in untagged_recaptures:
        tags = tags_by_code.get(code)
        if tags:
            table.add_capture(_tag_worn_on(table, tags, capture.day), capture)
    if columns["cicg_id"] is not None or other_code_column is not None:
        table.replaced = _written_as_lost(table, mentions) | _replaced_by_a_newer_tag(
            table, tags_by_code
        )
    return table


def _written_as_lost(table: ImportedTable, mentions: list[tuple[bytes, date | None]]) -> set[bytes]:
    lost: set[bytes] = set()
    for tag, day in mentions:
        if tag not in table.specimens or day is None:
            continue
        days = table.captures.get(tag)
        if not days or day >= max(days):
            lost.add(tag)
    return lost


def _row_date(
    table: ImportedTable,
    row: list[object],
    line: int,
    date_columns: list[tuple[int, Callable[[object], date | None]]],
) -> date | None:
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
            return day
        unreadable = unreadable or str(value)
    if unreadable is not None:
        table.warnings.append(f"Fila {line}: no se entiende la fecha ({unreadable}).")
    return None


def _first_day(table: ImportedTable, tag: bytes) -> date | None:
    days = table.captures.get(tag)
    return min(days) if days else None


def _tag_worn_on(table: ImportedTable, tags: set[bytes], day: date) -> bytes:
    """Of a bird's tags, the one it wore that day: the newest put on by then."""
    dated = sorted((first, tag) for tag in tags if (first := _first_day(table, tag)) is not None)
    worn = [tag for first, tag in dated if first <= day]
    if worn:
        return worn[-1]
    return dated[0][1] if dated else min(tags)


def _replaced_by_a_newer_tag(
    table: ImportedTable, tags_by_code: dict[str, set[bytes]]
) -> set[bytes]:
    replaced: set[bytes] = set()
    for tags in tags_by_code.values():
        if len(tags) < 2:
            continue
        for tag in tags:
            days = table.captures.get(tag)
            if not days:
                continue
            last = max(days)
            if any(
                (first := _first_day(table, other)) is not None and first > last
                for other in tags - {tag}
            ):
                replaced.add(tag)
    return replaced
