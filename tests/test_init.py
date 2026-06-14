"""Setup, unload and coordinator behaviour tests."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
import pytest

from custom_components.my_polenergia.hass_integration.coordinator import (
    PolEnergiaDataUpdateCoordinator,
)

# Patch target: keep the coordinator off the recorder during full setup.
_IMPORT_STATS = (
    "custom_components.my_polenergia.hass_integration.coordinator"
    ".PolEnergiaDataUpdateCoordinator.import_statistics"
)
from custom_components.my_polenergia.polenergia.errors import (
    PolEnergiaAuthorizationError,
    PolEnergiaConnectionError,
)

from .conftest import CUSTOMER_NUMBER, make_data


async def test_setup_and_unload(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Happy path: entry loads, sensors are created, then unloads cleanly."""
    mock_config_entry.add_to_hass(hass)

    with patch(_IMPORT_STATS, new=AsyncMock()):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    # Two sensors per measurement point: monthly consumption + import price.
    assert len(hass.states.async_entity_ids("sensor")) == 2

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_auth_failure_starts_reauth(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Bad credentials at setup → SETUP_ERROR and a reauth flow is started."""
    mock_client.authenticate.return_value = False
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(flow["context"]["source"] == "reauth" for flow in flows)


async def test_setup_connection_failure_not_loaded(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A connection error at setup leaves the entry not loaded (retry)."""
    mock_client.authenticate.side_effect = PolEnergiaConnectionError("down")
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state in (
        ConfigEntryState.SETUP_RETRY,
        ConfigEntryState.SETUP_ERROR,
    )


def _make_coordinator(hass, client, entry) -> PolEnergiaDataUpdateCoordinator:
    coord = PolEnergiaDataUpdateCoordinator(
        hass=hass,
        client=client,
        customer_number=CUSTOMER_NUMBER,
        update_interval=timedelta(hours=24),
        config_entry=entry,
    )
    # Isolate the fetch path from the recorder.
    coord.import_statistics = AsyncMock()
    return coord


async def test_token_expiry_reauth_then_refetch(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Expired token mid-refresh: re-login once, then refetch succeeds."""
    mock_config_entry.add_to_hass(hass)
    fresh = make_data()
    mock_client.get_all_data.side_effect = [
        PolEnergiaAuthorizationError("expired"),
        fresh,
    ]

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    result = await coord._async_update_data()

    assert result["data"] is fresh
    assert mock_client.authenticate.await_count == 1
    assert mock_client.get_all_data.await_count == 2


async def test_reauth_failure_raises_auth_failed(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """If the re-login fails, surface ConfigEntryAuthFailed (starts reauth)."""
    mock_config_entry.add_to_hass(hass)
    mock_client.get_all_data.side_effect = PolEnergiaAuthorizationError("expired")
    mock_client.authenticate.return_value = False

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()
