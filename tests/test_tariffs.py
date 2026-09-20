"""Tariff-group helpers and the tolerant readings parser.

Payload shapes are taken from real Polish operator APIs rather than invented:
the ``"A+ strefa 1"`` row strings and the positional ``zones`` array + legend
both come from Energa captures (see dev/multiple_tariffs.md).
"""

import pytest

from custom_components.my_polenergia.polenergia.data import (
    EnergyReading,
    aggregate_readings,
    parse_readings,
)
from custom_components.my_polenergia.polenergia.tariffs import (
    DIRECTION_EXPORT,
    DIRECTION_IMPORT,
    is_known_tariff,
    split_direction,
    zone_count,
    zone_display_name,
    zone_keys_for_count,
    zone_label_to_key,
)

DATE = "2025-01-31T00:00:00"


@pytest.mark.parametrize(
    ("tariff", "expected"),
    [
        ("G11", 1),
        ("G12", 2),
        ("G12w", 2),
        ("G12W", 2),
        ("G12as", 2),
        ("G12r", 2),
        ("G13", 3),
        (" g12w ", 2),
        (None, 1),
        ("", 1),
        ("X99", 1),  # unknown code must not invent zones
    ],
)
def test_zone_count(tariff, expected) -> None:
    """Zone count is prefix-matched, longest first, defaulting to single-zone."""
    assert zone_count(tariff) == expected


def test_zone_keys_for_count() -> None:
    """A single-zone tariff has no zone dimension at all."""
    assert zone_keys_for_count(1) == []
    assert zone_keys_for_count(2) == ["z1", "z2"]
    assert zone_keys_for_count(3) == ["z1", "z2", "z3"]


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("strefa 1", "z1"),
        ("Strefa 2 (nocna):", "z2"),
        ("Strefa całodobowa:", None),  # G11 label -> no zone split
        ("dzień", "z1"),
        ("noc", "z2"),
        (1, "z1"),
        ("Z1", "z1"),
        ("L2", "z2"),
        ("", None),
        ("total", None),
    ],
)
def test_zone_label_to_key(label, expected) -> None:
    """Zone labels from three different operator APIs map to stable slugs."""
    assert zone_label_to_key(label) == expected


def test_flat_rows_unchanged() -> None:
    """Today's Polenergia payload still parses to one unzoned import reading."""
    rows = [{"date": DATE, "amount": 171.0, "unit": "kWh", "measurementPointId": "mp1"}]
    readings, unknown = parse_readings(rows)

    assert len(readings) == 1
    assert readings[0].zone is None
    assert readings[0].direction == DIRECTION_IMPORT
    assert readings[0].value == 171.0
    assert readings[0].measurement_point_id == "mp1"
    assert not unknown


def test_long_form_one_row_per_zone() -> None:
    """Two rows sharing a date, split by zone label."""
    rows = [
        {"date": DATE, "amount": 100.0, "zone": "strefa 1"},
        {"date": DATE, "amount": 40.0, "zone": "strefa 2"},
    ]
    readings, _ = parse_readings(rows)

    assert [(r.zone, r.value) for r in readings] == [("z1", 100.0), ("z2", 40.0)]


def test_long_form_direction_marker() -> None:
    """Energa encodes direction and zone in one string: A+ import, A- export."""
    rows = [
        {"date": DATE, "amount": 100.0, "zone": "A+ strefa 1"},
        {"date": DATE, "amount": 9.0, "zone": "A- strefa 1"},
    ]
    readings, _ = parse_readings(rows)

    assert [(r.direction, r.zone, r.value) for r in readings] == [
        (DIRECTION_IMPORT, "z1", 100.0),
        (DIRECTION_EXPORT, "z1", 9.0),
    ]


def test_single_zone_label_yields_no_zone() -> None:
    """A whole-day label must not grow a z1 stream beside the total."""
    rows = [{"date": DATE, "amount": 171.0, "zone": "Strefa całodobowa:"}]
    readings, _ = parse_readings(rows)

    assert len(readings) == 1
    assert readings[0].zone is None


def test_wide_form_named_fields() -> None:
    """One row carrying amountZone1/amountZone2 expands into two readings."""
    rows = [{"date": DATE, "amountZone1": 100.0, "amountZone2": 40.0}]
    readings, unknown = parse_readings(rows)

    assert [(r.zone, r.value) for r in readings] == [("z1", 100.0), ("z2", 40.0)]
    assert not unknown  # the expanded keys are recognised, not "unknown"


def test_wide_form_positional_array_with_legend() -> None:
    """Energa chart shape: positional zones array plus an envelope legend."""
    rows = [{"date": DATE, "zones": [None, 40.0]}]
    envelope = {
        "zones": [
            {"index": 0, "label": "Strefa 1 (dzienna):"},
            {"index": 1, "label": "Strefa 2 (nocna):"},
        ]
    }
    readings, _ = parse_readings(rows, envelope)

    assert len(readings) == 1
    assert readings[0].zone == "z2"
    assert readings[0].value == 40.0


def test_wide_form_positional_single_zone() -> None:
    """A one-element zones array (G11) collapses to no zone."""
    rows = [{"date": DATE, "zones": [171.0]}]
    envelope = {"zones": [{"index": 0, "label": "Strefa całodobowa:"}]}
    readings, _ = parse_readings(rows, envelope)

    assert len(readings) == 1
    assert readings[0].zone is None


def test_wide_form_dict_with_names() -> None:
    """Tauron shape: a zones dict plus a display-name map."""
    rows = [
        {
            "date": DATE,
            "zones": {"a": 100.0, "b": 40.0},
            "zonesName": {"a": "Dzień", "b": "Noc"},
        }
    ]
    readings, _ = parse_readings(rows)

    assert sorted((r.zone, r.value) for r in readings) == [("z1", 100.0), ("z2", 40.0)]


def test_negative_amount_becomes_export() -> None:
    """A negative import value is a feed-in, not negative consumption."""
    rows = [{"date": DATE, "amount": -12.5}]
    readings, _ = parse_readings(rows)

    assert readings[0].direction == DIRECTION_EXPORT
    assert readings[0].value == 12.5


def test_unknown_fields_are_captured() -> None:
    """Unrecognised keys surface for diagnostics instead of being dropped silently."""
    rows = [{"date": DATE, "amount": 1.0, "surpriseField": "x"}]
    readings, unknown = parse_readings(rows)

    assert len(readings) == 1
    assert unknown == {"surpriseField"}


def test_row_without_timestamp_is_skipped() -> None:
    """A row with no usable anchor is skipped rather than fabricating 'now'."""
    readings, _ = parse_readings([{"amount": 1.0}, {"date": DATE, "amount": 2.0}])

    assert len(readings) == 1
    assert readings[0].value == 2.0


def test_aggregate_collapses_duplicate_periods() -> None:
    """Rows sharing (meter, period, zone, direction) sum into one reading.

    Guards the duplicate StatisticData.start bug: two points with the same start
    would otherwise corrupt the cumulative sum.
    """
    readings, _ = parse_readings(
        [
            {"date": DATE, "amount": 100.0, "measurementPointId": "mp1"},
            {"date": DATE, "amount": 50.0, "measurementPointId": "mp1"},
        ]
    )
    aggregated = aggregate_readings(readings)

    assert len(aggregated) == 1
    assert aggregated[0].value == 150.0


def test_aggregate_keeps_zones_and_directions_apart() -> None:
    """Aggregation must not merge different zones or directions."""
    readings, _ = parse_readings(
        [
            {"date": DATE, "amount": 100.0, "zone": "A+ strefa 1", "measurementPointId": "mp1"},
            {"date": DATE, "amount": 40.0, "zone": "A+ strefa 2", "measurementPointId": "mp1"},
            {"date": DATE, "amount": 9.0, "zone": "A- strefa 1", "measurementPointId": "mp1"},
        ]
    )
    aggregated = aggregate_readings(readings)

    assert len(aggregated) == 3


@pytest.mark.parametrize(
    ("code", "expected"),
    [("G11", True), ("g12w", True), ("X99", False), (None, False), ("", False)],
)
def test_is_known_tariff(code, expected: bool) -> None:
    """Only real G-group prefixes count as recognised."""
    assert is_known_tariff(code) is expected


def test_split_direction_empty_label_defaults_to_import() -> None:
    """A blank label carries no marker, so it is an import row."""
    assert split_direction("") == (DIRECTION_IMPORT, "")
    assert split_direction("   ") == (DIRECTION_IMPORT, "")


def test_zone_label_to_key_slugifies_unknown_labels() -> None:
    """An unrecognised label keeps a stable slug rather than being dropped."""
    assert zone_label_to_key("Przedpoludnie ranne") == "przedpoludnie_ranne"
    assert zone_label_to_key("!!!") is None



@pytest.mark.parametrize(
    ("zone", "expected"), [(None, ""), ("z1", "Day"), ("szczyt_poranny", "Szczyt Poranny")]
)
def test_zone_display_name(zone, expected: str) -> None:
    """Known zones get their label; unknown slugs are title-cased."""
    assert zone_display_name(zone) == expected


def test_explicit_direction_field() -> None:
    """A dedicated direction field wins over guessing from the amount."""
    readings, _ = parse_readings(
        [{"date": DATE, "amount": 30.0, "direction": "A-", "measurementPointId": "mp1"}]
    )
    assert readings[0].direction == DIRECTION_EXPORT

    readings, _ = parse_readings(
        [{"date": DATE, "amount": 30.0, "direction": "A+", "measurementPointId": "mp1"}]
    )
    assert readings[0].direction == DIRECTION_IMPORT


def test_legend_entries_that_are_plain_strings() -> None:
    """A legend given as a list of strings maps positionally."""
    readings, _ = parse_readings(
        [{"date": DATE, "zones": [10.0, 20.0], "measurementPointId": "mp1"}],
        {"zones": ["Strefa 1 (dzienna):", "Strefa 2 (nocna):"]},
    )
    assert sorted((r.zone, r.value) for r in readings) == [("z1", 10.0), ("z2", 20.0)]


def test_legend_with_unusable_index_is_skipped() -> None:
    """A legend entry with a non-numeric index does not break the parse."""
    readings, _ = parse_readings(
        [{"date": DATE, "zones": [10.0, 20.0], "measurementPointId": "mp1"}],
        {"zones": [{"index": "x", "label": "Strefa 1:"}, {"index": 1, "label": "Strefa 2:"}]},
    )
    assert {r.zone for r in readings} == {"z1", "z2"}


def test_non_dict_rows_are_skipped() -> None:
    """A row that is not an object yields nothing rather than raising."""
    readings, unknown = parse_readings(["nonsense", None])
    assert readings == []
    assert unknown == set()


def test_row_with_no_value_yields_zero() -> None:
    """A row with a timestamp but no amount is a zero-kWh period, not a crash."""
    readings, _ = parse_readings([{"date": DATE, "measurementPointId": "mp1"}])
    assert [r.value for r in readings] == [0.0]


def test_timestamp_fallback_to_fromisoformat() -> None:
    """A timestamp in an offset format none of the patterns match still parses."""
    readings, _ = parse_readings(
        [{"date": "2025-01-31T00:00:00+01:00", "amount": 5.0, "measurementPointId": "mp1"}]
    )
    assert readings[0].timestamp.tzinfo is not None


def test_from_api_response_single_row() -> None:
    """The single-row helper returns one reading, or raises on an unusable row."""
    reading = EnergyReading.from_api_response(
        {"date": DATE, "amount": 42.0, "measurementPointId": "mp1"}
    )
    assert reading.value == 42.0
    assert reading.measurement_point_id == "mp1"

    with pytest.raises(ValueError, match="no timestamp"):
        EnergyReading.from_api_response({"amount": 1.0})
