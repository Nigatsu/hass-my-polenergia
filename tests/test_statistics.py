"""Statistics import logic tests (coordinator.import_statistics)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.my_polenergia.const import DOMAIN
from custom_components.my_polenergia.hass_integration.coordinator import (
    PolEnergiaDataUpdateCoordinator,
)

from .conftest import (
    CUSTOMER_NUMBER,
    make_data,
    make_measurement_point,
    make_reading,
)

_ADD_STATS = (
    "custom_components.my_polenergia.hass_integration.coordinator"
    ".async_add_external_statistics"
)


def _make_coordinator(hass, client, entry) -> PolEnergiaDataUpdateCoordinator:
    return PolEnergiaDataUpdateCoordinator(
        hass=hass,
        client=client,
        customer_number=CUSTOMER_NUMBER,
        update_interval=timedelta(hours=24),
        config_entry=entry,
    )


def _streams(add_mock) -> tuple[dict, dict]:
    """Split captured async_add_external_statistics calls into energy/cost maps."""
    energy: dict[str, list] = {}
    cost: dict[str, list] = {}
    for call in add_mock.call_args_list:
        _hass, metadata, stats = call.args
        sid = metadata["statistic_id"]
        if sid.endswith("_energy"):
            energy[sid] = stats
        elif sid.endswith("_cost"):
            cost[sid] = stats
    return energy, cost


async def test_first_import_cumulative_sums(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """First import builds monotonically increasing cumulative sums from zero."""
    data = make_data([make_measurement_point("mp1")])
    mock_client.get_readings.return_value = [
        make_reading(2024, 1, 100.0),
        make_reading(2024, 2, 150.0),
        make_reading(2024, 3, 120.0),
    ]

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord._last_stat_sum = AsyncMock(return_value=None)  # first run

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(data)

    energy, _ = _streams(add_mock)
    sums = [point["sum"] for point in energy[f"{DOMAIN}:mp1_energy"]]
    # 3 readings (100, 250, 370) + a zero-delta current-month anchor (370).
    assert sums == [100.0, 250.0, 370.0, 370.0]
    assert sums == sorted(sums)


async def test_resume_continues_without_duplication(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A resume only appends months newer than the last stored one."""
    data = make_data([make_measurement_point("mp1")])
    mock_client.get_readings.return_value = [
        make_reading(2024, 1, 100.0),
        make_reading(2024, 2, 150.0),
        make_reading(2024, 3, 120.0),
    ]

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    # Last stored point is February with cumulative sum 250.
    last_ts = datetime(2024, 2, 28, tzinfo=UTC).timestamp()
    coord._last_stat_sum = AsyncMock(return_value=(250.0, last_ts))

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(data)

    energy, _ = _streams(add_mock)
    sums = [point["sum"] for point in energy[f"{DOMAIN}:mp1_energy"]]
    # Only March is new: 250 + 120 = 370, plus the current-month anchor.
    assert sums == [370.0, 370.0]


async def test_cost_is_energy_times_price(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """The cost stream equals energy × the configured price (default 0.95)."""
    data = make_data([make_measurement_point("mp1")])
    mock_client.get_readings.return_value = [make_reading(2024, 1, 100.0)]

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord._last_stat_sum = AsyncMock(return_value=None)

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(data)

    energy, cost = _streams(add_mock)
    energy_sums = [p["sum"] for p in energy[f"{DOMAIN}:mp1_energy"]]
    cost_sums = [p["sum"] for p in cost[f"{DOMAIN}:mp1_cost"]]
    assert energy_sums[0] == pytest.approx(100.0)
    assert cost_sums[0] == pytest.approx(95.0)
    assert cost_sums[0] == pytest.approx(energy_sums[0] * 0.95)


async def test_multi_meter_readings_split_per_point(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Each meter's stream contains only its own readings (Phase 2 regression)."""
    mp1 = make_measurement_point("mp1", ppe="PL0001")
    mp2 = make_measurement_point("mp2", ppe="PL0002")
    data = make_data([mp1, mp2])
    mock_client.get_readings.return_value = [
        make_reading(2024, 1, 100.0, "mp1"),
        make_reading(2024, 1, 500.0, "mp2"),
        make_reading(2024, 2, 150.0, "mp1"),
        make_reading(2024, 2, 600.0, "mp2"),
    ]

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord._last_stat_sum = AsyncMock(return_value=None)

    with patch(_ADD_STATS) as add_mock:
        await coord.import_statistics(data)

    energy, _ = _streams(add_mock)
    mp1_sums = [p["sum"] for p in energy[f"{DOMAIN}:mp1_energy"]]
    mp2_sums = [p["sum"] for p in energy[f"{DOMAIN}:mp2_energy"]]
    # mp1: 100, 250 (not polluted by mp2's 500/600).
    assert mp1_sums[:2] == [100.0, 250.0]
    # mp2: 500, 1100.
    assert mp2_sums[:2] == [500.0, 1100.0]
