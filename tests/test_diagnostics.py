"""Diagnostics output: tariff evidence capture and PII redaction."""

from unittest.mock import MagicMock

from homeassistant.core import HomeAssistant

from custom_components.my_polenergia.diagnostics import (
    async_get_config_entry_diagnostics,
)

from .conftest import make_data, make_measurement_point


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
