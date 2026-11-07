"""BioMark is a transformation of the tag's bytes, checked here against real
pairs from the CICG's own cleaned data (feeder-rfid-legacy)."""

import pytest

from feeder_station.domain.tags import parse_tag, to_biomark


@pytest.mark.parametrize(
    ("hex_tag", "biomark"),
    [
        ("03843D3C797055", "900_263007596629"),
        ("03843D3C2D199F", "900_263002593695"),
        ("03843D3C11B957", "900_263000799575"),
        ("0384199C87EC06", "900_110000335878"),
        # The 901 batch: the leading zero is part of the code.
        ("038504E3B6461B", "901_021000242715"),
    ],
)
def test_biomark_matches_the_cicg_data_both_ways(hex_tag: str, biomark: str) -> None:
    tag = bytes.fromhex(hex_tag)
    assert to_biomark(tag) == biomark
    assert parse_tag(biomark) == tag
    assert parse_tag(hex_tag) == tag


def test_a_prefix_other_than_900_is_transformed_too() -> None:
    assert to_biomark(bytes.fromhex("03D9000000000A")) == "985_000000000010"
    assert parse_tag("985_10") == bytes.fromhex("03D9000000000A")


def test_a_code_written_without_its_leading_zero_is_the_same_tag() -> None:
    assert parse_tag("901_21000242715") == parse_tag("901_021000242715")


def test_a_spreadsheet_that_lost_the_separator_still_reads() -> None:
    assert parse_tag("900263007596629") == bytes.fromhex("03843D3C797055")


@pytest.mark.parametrize("bad", ["", "abc", "900", "99999_1", "900_99999999999999", "12345"])
def test_what_is_not_a_tag_is_refused(bad: str) -> None:
    assert parse_tag(bad) is None
