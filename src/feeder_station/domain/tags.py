"""Tag identifiers in the format the CICG uses: BioMark.

The RFID module and the radio carry a tag as 7 bytes (14 hex characters). A
person never should: the capture log and every analysis use BioMark, a
transformation of those same bytes, not a prefix plus the hex. The first 2 bytes
and the last 5 are each written in decimal and joined by ``_``, so
``0384199C87EC06`` is ``900_110000335878``. Same formula as
``feeder-rfid-legacy``'s ``create_biomark_column``.
"""

from __future__ import annotations

import re

from feeder_station.domain.payloads import TAG_BYTES

_BIOMARK = re.compile(r"(\d{1,5})[_ ](\d{1,13})")
_PREFIX_BYTES = 2
_SUFFIX_BYTES = TAG_BYTES - _PREFIX_BYTES
_UNSEPARATED_PREFIX_DIGITS = 3
_UNSEPARATED_DIGITS = 15


def to_biomark(tag: bytes) -> str:
    if len(tag) != TAG_BYTES:
        raise ValueError(f"a tag is {TAG_BYTES} bytes, got {len(tag)}")
    prefix = int.from_bytes(tag[:_PREFIX_BYTES], "big")
    suffix = int.from_bytes(tag[_PREFIX_BYTES:], "big")
    return f"{prefix}_{suffix}"


def _from_parts(prefix: int, suffix: int) -> bytes | None:
    if prefix >= 1 << (8 * _PREFIX_BYTES) or suffix >= 1 << (8 * _SUFFIX_BYTES):
        return None
    return prefix.to_bytes(_PREFIX_BYTES, "big") + suffix.to_bytes(_SUFFIX_BYTES, "big")


def parse_tag(text: str) -> bytes | None:
    """Read a tag as a person or a spreadsheet wrote it, or ``None``.

    Accepts BioMark (``900_110000335878``), BioMark with its separator lost to a
    spreadsheet (15 digits), and the raw 14 hex characters.
    """
    value = text.strip()
    match = _BIOMARK.fullmatch(value)
    if match:
        return _from_parts(int(match[1]), int(match[2]))
    if value.isdigit() and len(value) == _UNSEPARATED_DIGITS:
        return _from_parts(
            int(value[:_UNSEPARATED_PREFIX_DIGITS]), int(value[_UNSEPARATED_PREFIX_DIGITS:])
        )
    if len(value) == TAG_BYTES * 2:
        try:
            return bytes.fromhex(value)
        except ValueError:
            return None
    return None
