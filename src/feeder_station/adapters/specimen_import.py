"""Read the CICG's list of tagged individuals from an Excel or CSV file.

The real list has one row per capture event, so a tag can repeat; the newest
non-empty value of each field wins, and a tag listed under two species is
reported rather than silently resolved. The ``PIT`` column holds BioMark.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

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


@dataclass
class ImportedTable:
    specimens: dict[bytes, dict[str, str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


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
    return table
