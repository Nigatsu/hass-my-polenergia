"""Sensor platform: values, attributes, availability and dynamic meters."""

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms

from custom_components.my_polenergia.const import CONF_IMPORT_PRICE

from .conftest import (
    ACCOUNT_NAME,
    CUSTOMER_NUMBER,
    make_data,
    make_measurement_point,
    make_reading,
)

_IMPORT_STATS = (
    "custom_components.my_polenergia.coordinator"
    ".PolEnergiaDataUpdateCoordinator.import_statistics"
)


async def _setup(hass: HomeAssistant, entry) -> None:
    """Load the entry with the statistics import stubbed out."""
    entry.add_to_hass(hass)
    with patch(_IMPORT_STATS, new=AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


def _entity_id(hass: HomeAssistant, unique_id: str) -> str | None:
    """Resolve a sensor entity_id from its unique_id."""
    return er.async_get(hass).async_get_entity_id("sensor", "my_polenergia", unique_id)


def _platform_entities(hass: HomeAssistant) -> list:
    """The live sensor entity objects of this integration."""
    return [
        entity
        for platform in async_get_platforms(hass, "my_polenergia")
        for entity in platform.entities.values()
    ]


async def _refresh(hass: HomeAssistant, entry) -> None:
    """Trigger a coordinator refresh without touching the recorder."""
    with patch(_IMPORT_STATS, new=AsyncMock()):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()


async def test_consumption_sensor_value_and_attributes(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """The consumption sensor shows the latest month and its metadata."""
    mp = make_measurement_point("mp1", ppe="PL0001", address="Main St 1")
    mock_client.get_all_data.return_value = make_data(
        [mp],
        {"mp1": [make_reading(2024, 1, 100.0, "mp1"), make_reading(2024, 2, 150.0, "mp1")]},
    )

    await _setup(hass, mock_config_entry)

    state = hass.states.get(_entity_id(hass, "mp1_reading"))
    assert float(state.state) == 150.0
    assert state.attributes["period"] == "2024-02"
    assert state.attributes["ppe"] == "PL0001"
    assert state.attributes["address"] == "Main St 1"
    assert state.attributes["customer_number"] == CUSTOMER_NUMBER
    assert state.attributes["account_name"] == ACCOUNT_NAME
    assert state.attributes["tariff"] == "G11"
    assert state.attributes["zone_count"] == 1
    assert "last_update" in state.attributes
    # Single non-cumulative month: a state_class would poison the dashboard.
    assert "state_class" not in state.attributes


async def test_consumption_sensor_without_readings_is_unknown(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A meter with no readings reports unknown rather than a stale number."""
    mock_client.get_all_data.return_value = make_data([make_measurement_point("mp1")])

    await _setup(hass, mock_config_entry)

    assert hass.states.get(_entity_id(hass, "mp1_reading")).state == "unknown"


async def test_consumption_sensor_sums_zones_of_latest_period(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A zoned meter shows the month's total, not just one zone's row."""
    mock_client.get_all_data.return_value = make_data(
        [make_measurement_point("mp1")],
        {
            "mp1": [
                make_reading(2024, 2, 90.0, "mp1", zone="z1"),
                make_reading(2024, 2, 60.0, "mp1", zone="z2"),
            ]
        },
    )

    await _setup(hass, mock_config_entry)

    assert float(hass.states.get(_entity_id(hass, "mp1_reading")).state) == 150.0


async def test_price_sensor_reads_options(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """The diagnostic price sensor mirrors the configured rate."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_IMPORT_PRICE: 1.23}
    )
    with patch(_IMPORT_STATS, new=AsyncMock()):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert float(hass.states.get(_entity_id(hass, "mp1_import_price")).state) == 1.23


async def test_price_sensor_falls_back_to_default(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """With no price configured, the placeholder rate is shown."""
    await _setup(hass, mock_config_entry)

    assert float(hass.states.get(_entity_id(hass, "mp1_import_price")).state) == 0.95


async def test_entity_unavailable_when_meter_missing_from_data(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """An entity whose measurement point left the payload reports unavailable.

    Stale meters are normally pruned with their device, but until that happens
    the entity must not keep serving its last known value.
    """
    mp1 = make_measurement_point("mp1", ppe="PL0001", address="Main St 1")
    mp2 = make_measurement_point("mp2", ppe="PL0002", address="Side St 2")
    mock_client.get_all_data.return_value = make_data([mp1, mp2])

    await _setup(hass, mock_config_entry)

    coordinator = mock_config_entry.runtime_data
    entities = _platform_entities(hass)
    mp2_entity = next(e for e in entities if e.measurement_point.id == "mp2")
    assert mp2_entity.available

    # The coordinator succeeded, but this meter is no longer in the payload.
    coordinator.data = {"data": make_data([mp1])}
    assert not mp2_entity.available
    assert next(e for e in entities if e.measurement_point.id == "mp1").available

    # No data at all is also unavailable, rather than a stale reading.
    coordinator.data = None
    assert not mp2_entity.available


async def test_new_measurement_point_adds_entities(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A meter added to the account later appears without a manual reload."""
    mp1 = make_measurement_point("mp1", ppe="PL0001", address="Main St 1")
    mock_client.get_all_data.return_value = make_data([mp1])

    await _setup(hass, mock_config_entry)
    assert len(hass.states.async_entity_ids("sensor")) == 2

    mp2 = make_measurement_point("mp2", ppe="PL0002", address="Side St 2")
    mock_client.get_all_data.return_value = make_data([mp1, mp2])
    await _refresh(hass, mock_config_entry)

    assert len(hass.states.async_entity_ids("sensor")) == 4
    assert hass.states.get(_entity_id(hass, "mp2_reading")) is not None
