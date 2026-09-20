"""Diagnostics output: tariff evidence capture and PII redaction."""

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.my_polenergia.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import make_data, make_measurement_point, make_reading


async def test_diagnostics_redacts_pii_and_reports_tariff_evidence(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Address/PPE are redacted; zone evidence is included for bug reports."""
    mock_config_entry.add_to_hass(hass)

    data = make_data([make_measurement_point("mp1")])
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = {"data": data}
    coordinator.zones_seen = {"mp1": ["z1", "z2"]}
    coordinator.client = MagicMock(
        last_unknown_reading_fields={"surpriseField"},
        last_envelope_keys=["results", "zones"],
        last_reading_sample=[
            {
                "date": "2025-01-31T00:00:00",
                "amount": 171.0,
                "zone": "A+ strefa 1",
                "measurementPointId": "140017222",
            }
        ],
    )
    mock_config_entry.runtime_data = coordinator

    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert result["data"]["measurement_points"][0]["address"] == "**REDACTED**"
    assert result["data"]["measurement_points"][0]["ppe"] == "**REDACTED**"
    assert result["data"]["measurement_points"][0]["tariff"] == "G11"
    assert result["data"]["measurement_points"][0]["zone_count"] == 1

    tariffs = result["tariffs"]
    assert tariffs["zones_seen"] == {"mp1": ["z1", "z2"]}
    assert tariffs["unknown_reading_fields"] == ["surpriseField"]
    assert tariffs["readings_envelope_keys"] == ["results", "zones"]

    sample = tariffs["reading_sample"][0]
    # Numbers and zone labels survive; other identifying strings do not.
    assert sample["amount"] == 171.0
    assert sample["zone"] == "A+ strefa 1"
    assert sample["date"] == "2025-01-31T00:00:00"
    assert sample["measurementPointId"] == "**REDACTED**"


async def test_diagnostics_redacts_nested_structures(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Redaction reaches into nested objects and lists in the sample row."""
    mock_config_entry.add_to_hass(hass)

    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = None
    coordinator.zones_seen = {}
    coordinator.client = MagicMock(
        last_unknown_reading_fields=set(),
        last_envelope_keys=[],
        last_reading_sample=[
            {
                "date": "2025-01-31T00:00:00",
                "meta": {"ppeNumber": "590000000000000000", "count": 2},
                "rows": [{"serial": "SN-1", "amount": 3.0}, 7],
                "zones": [None, 0.374],
            }
        ],
    )
    mock_config_entry.runtime_data = coordinator

    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    sample = result["tariffs"]["reading_sample"][0]
    assert sample["meta"] == {"ppeNumber": "**REDACTED**", "count": 2}
    assert sample["rows"] == [{"serial": "**REDACTED**", "amount": 3.0}, 7]
    assert sample["zones"] == [None, 0.374]
    # No coordinator data at all: the optional block is simply absent.
    assert "data" not in result


async def test_diagnostics_reports_reading_summary(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Auth scope state and a per-meter reading summary reach the dump."""
    mock_config_entry.add_to_hass(hass)

    mp = make_measurement_point("mp1")
    data = make_data(
        [mp],
        {
            "mp1": [
                make_reading(2024, 1, 100.0, "mp1", zone="z1"),
                make_reading(2024, 2, 150.0, "mp1", zone="z2"),
                make_reading(2024, 2, 20.0, "mp1", direction="export"),
            ]
        },
    )
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = {"data": data}
    coordinator.zones_seen = {"mp1": ["z1", "z2"]}
    coordinator.client = MagicMock(
        last_unknown_reading_fields=set(),
        last_envelope_keys=[],
        last_reading_sample=[],
    )
    mock_config_entry.runtime_data = coordinator

    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    summary = result["data"]["readings_summary"]["mp1"]
    assert summary["count"] == 3
    assert summary["first_period"].startswith("2024-01")
    assert summary["last_period"].startswith("2024-02")
    assert summary["zones"] == ["z1", "z2"]
    assert sorted(summary["directions"]) == ["export", "import"]
    # The summary describes the data without disclosing any of it.
    assert "100.0" not in str(summary)


async def test_diagnostics_summary_for_meter_without_readings(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """An empty meter is reported as an explicit zero, not omitted."""
    mock_config_entry.add_to_hass(hass)

    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.data = {"data": make_data([make_measurement_point("mp1")])}
    coordinator.zones_seen = {}
    coordinator.client = MagicMock(
        last_unknown_reading_fields=set(),
        last_envelope_keys=[],
        last_reading_sample=[],
    )
    mock_config_entry.runtime_data = coordinator

    result = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert result["data"]["readings_summary"]["mp1"] == {
        "count": 0,
        "first_period": None,
        "last_period": None,
        "zones": [],
    }
