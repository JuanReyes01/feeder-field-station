"""What the CICG writes down at each capture, and how a bird's measures vary.

The individuals database has one row per capture: the morphometrics, the molt
scores, who handled the bird and how, the sample and the photos taken. Each
column is read under its own key here, whatever spelling the file uses, so the
station can show one capture's whole record and compare the same measure
across recaptures.

A measure's variation is told against the bird's own median: a value far from
it is either real (weight changes with the season, wing with molt) or a typo,
and either way worth a look. How far is "far" is the operator's call, so every
value carries its deviation and the threshold lives in the interface.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class CaptureField:
    key: str
    label: str
    group: str
    aliases: tuple[str, ...]
    # number, text, time (a clock time) or flag (yes when ticked)
    kind: str = "text"
    unit: str | None = None


GROUPS: tuple[tuple[str, str], ...] = (
    ("medidas", "Medidas"),
    ("muda", "Muda"),
    ("captura", "Captura"),
    ("muestra", "Muestra"),
    ("fotos", "Fotos"),
)


def _measure(key: str, label: str, *aliases: str, unit: str = "mm") -> CaptureField:
    return CaptureField(key, label, "medidas", aliases, kind="number", unit=unit)


def _molt(key: str, label: str, *aliases: str) -> CaptureField:
    return CaptureField(key, label, "muda", aliases)


def _photo(key: str, label: str, *aliases: str) -> CaptureField:
    return CaptureField(key, label, "fotos", aliases, kind="flag")


# Column names as the CICG's database writes them (lower case), plus Spanish.
CAPTURE_FIELDS: tuple[CaptureField, ...] = (
    _measure("weight", "Peso", "weight", "peso", unit="g"),
    _measure("wing", "Ala", "wing length", "wing", "ala"),
    _measure("tail_length", "Cola", "tail length", "cola"),
    _measure("exposed_culmen", "Culmen expuesto", "exposed culmen", "culmen expuesto"),
    _measure("total_culmen", "Culmen total", "total culmen", "culmen total"),
    _measure("bill_height", "Alto del pico", "bill height", "alto del pico"),
    _measure("bill_width", "Ancho del pico", "bill width", "ancho del pico"),
    _measure("commissure", "Comisura", "commisure", "commissure", "comisura"),
    _measure("tarsus", "Tarso", "tarsus", "tarso"),
    _measure("hallux_claw", "Garra del hallux", "halux claw", "hallux claw"),
    _measure("middle_claw", "Garra del dedo medio", "middle fing claw", "middle finger claw"),
    _measure("foot_extension", "Extensión del pie", "foot ext", "foot extension"),
    _measure(
        "foot_extension_electronic",
        "Extensión del pie (electrónico)",
        "foot ext electronic",
        "foot ext2",
    ),
    _measure("keel", "Quilla", "keel", "quilla"),
    _measure("chest_width", "Ancho del pecho", "chest width", "ancho del pecho"),
    _molt("molt_primaries", "Primarias", "primaries", "primarias"),
    _molt("molt_secondaries", "Secundarias", "secondaries", "secundarias"),
    _molt("molt_tail", "Cola", "tail"),
    _molt("molt_nape", "Nuca", "nape", "nuca"),
    _molt("molt_throat", "Garganta", "throat", "garganta"),
    _molt("molt_crown", "Corona", "crown", "corona"),
    _molt("molt_belly", "Vientre", "belly", "vientre"),
    _molt("molt_back", "Espalda", "back", "espalda"),
    _molt("wrp", "Patrón de reemplazo (WRP)", "wrp"),
    _molt("molt_comments", "Comentarios de muda", "comments molt", "comentarios muda"),
    CaptureField("age", "Edad", "captura", ("age", "edad")),
    CaptureField("sex", "Sexo", "captura", ("sex", "sexo")),
    CaptureField("sexing_method", "Método de sexado", "captura", ("sexing method",)),
    CaptureField("brood_patch", "Parche de incubación", "captura", ("bp (brood patch)", "bp")),
    CaptureField(
        "time_captured", "Hora de captura", "captura", ("time captured", "hora captura"), "time"
    ),
    CaptureField(
        "time_released",
        "Hora de liberación",
        "captura",
        ("time released", "hora liberación"),
        "time",
    ),
    CaptureField("method", "Método de captura", "captura", ("método capt", "metodo capt")),
    CaptureField("tagged_by", "Marcado por", "captura", ("tagged by",)),
    CaptureField("measured_by", "Medido por", "captura", ("measured by",)),
    CaptureField("gauge_ring", "Calibre del anillo", "captura", ("gauge ring",)),
    CaptureField("permit", "Permiso", "captura", ("permiso", "permit")),
    CaptureField("comments", "Comentarios", "captura", ("comments", "comentarios")),
    CaptureField("sample", "Muestra tomada", "muestra", ("sample (y/n)", "sample")),
    CaptureField("sample_type", "Tipo de muestra", "muestra", ("type",)),
    CaptureField("sample_code", "Código de muestra", "muestra", ("code",)),
    _photo("photo_wing", "Ala", "wing ph"),
    _photo("photo_tail", "Cola", "tail ph"),
    _photo("photo_bill_views", "Pico, 3 vistas", "bill 3 views"),
    _photo("photo_bill_tip", "Punta del pico", "bill tip"),
    _photo("photo_serrations", "Serraciones", "serrations ph"),
    _photo("photo_foot", "Extensión del pie", "foot ext ph"),
    _photo("photo_claws", "Garras", "claws ph"),
    _photo("photo_body", "Cuerpo en mano", "body in hand"),
)

MEASURES: tuple[CaptureField, ...] = tuple(f for f in CAPTURE_FIELDS if f.kind == "number")


@dataclass(frozen=True)
class MeasuredValue:
    day: str
    value: float
    # (value - median) / median, the bird's own median for this measure.
    deviation: float | None


@dataclass(frozen=True)
class MeasureSummary:
    count: int
    first: float
    last: float
    minimum: float
    maximum: float
    mean: float
    median: float
    # Sample standard deviation and coefficient of variation: None below two values.
    stdev: float | None
    cv: float | None
    change: float  # last - first
    values: tuple[MeasuredValue, ...]


def summarize(values: Sequence[tuple[str, float]]) -> MeasureSummary | None:
    """One measure across a bird's captures, given oldest first as (day, value)."""
    numbers = [value for _day, value in values if math.isfinite(value)]
    if not numbers:
        return None
    dated = [(day, value) for day, value in values if math.isfinite(value)]
    median = statistics.median(numbers)
    mean = statistics.fmean(numbers)
    stdev = statistics.stdev(numbers) if len(numbers) > 1 else None
    return MeasureSummary(
        count=len(numbers),
        first=numbers[0],
        last=numbers[-1],
        minimum=min(numbers),
        maximum=max(numbers),
        mean=mean,
        median=median,
        stdev=stdev,
        cv=stdev / mean if stdev is not None and mean else None,
        change=numbers[-1] - numbers[0],
        values=tuple(
            MeasuredValue(day, value, (value - median) / median if median else None)
            for day, value in dated
        ),
    )
