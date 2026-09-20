"""Domain services: input validation and failure propagation."""

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.my_polenergia.const import DOMAIN

from .conftest import make_data, make_measurement_point

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


async def test_reload_statistics_rejects_bad_date(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A malformed from_date is a user error, not a silent no-op."""
    await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "reload_statistics", {"from_date": "nonsense"}, blocking=True
        )
    assert err.value.translation_key == "invalid_from_date"


async def test_reload_statistics_passes_parsed_date(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A valid date reaches the importer as an aware datetime."""
    await _setup(hass, mock_config_entry)

    with patch(_IMPORT_STATS, new=AsyncMock()) as import_mock:
        await hass.services.async_call(
            DOMAIN, "reload_statistics", {"from_date": "2020-01-01"}, blocking=True
        )

    from_date = import_mock.await_args.kwargs["from_date"]
    assert from_date.tzinfo is not None
    assert from_date.year == 2020
    assert import_mock.await_args.kwargs["full_rebuild"] is True


async def test_reload_statistics_surfaces_import_failure(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """An import failure reaches the caller instead of only the log."""
    await _setup(hass, mock_config_entry)

    with (
        patch(_IMPORT_STATS, new=AsyncMock(side_effect=RuntimeError("recorder down"))),
        pytest.raises(HomeAssistantError) as err,
    ):
        await hass.services.async_call(DOMAIN, "reload_statistics", {}, blocking=True)
    assert err.value.translation_key == "reload_failed"


async def test_clear_statistics_without_streams_raises(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Nothing to clear is reported, not warned about and swallowed."""
    mock_client.get_all_data.return_value = make_data([])
    await _setup(hass, mock_config_entry)

    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "clear_statistics", {}, blocking=True)
    assert err.value.translation_key == "no_statistics_to_clear"


async def test_clear_statistics_clears_both_streams(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Every measurement point's energy and cost streams are cleared."""
    mock_client.get_all_data.return_value = make_data([make_measurement_point("mp1")])
    await _setup(hass, mock_config_entry)

    with patch(
        "custom_components.my_polenergia.get_instance"
    ) as get_instance:
        await hass.services.async_call(DOMAIN, "clear_statistics", {}, blocking=True)

    cleared = get_instance.return_value.async_clear_statistics.call_args.args[0]
    assert set(cleared) == {f"{DOMAIN}:mp1_energy", f"{DOMAIN}:mp1_cost"}
