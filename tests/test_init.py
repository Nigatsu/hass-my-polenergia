"""Setup, unload and coordinator behaviour tests."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.my_polenergia.const import (
    CONF_ACCOUNT_NAME,
    CONF_CUSTOMER_NUMBER,
    CONF_IMPORT_PRICE,
    DEFAULT_IMPORT_PRICE,
    DOMAIN,
)
from custom_components.my_polenergia.coordinator import (
    PolEnergiaDataUpdateCoordinator,
)
from custom_components.my_polenergia.polenergia.errors import (
    PolEnergiaAPIError,
    PolEnergiaAuthorizationError,
    PolEnergiaConnectionError,
)

from .conftest import ACCOUNT_NAME, CUSTOMER_NUMBER, PASSWORD, USERNAME, make_data

# Patch target: keep the coordinator off the recorder during full setup.
_IMPORT_STATS = (
    "custom_components.my_polenergia.coordinator"
    ".PolEnergiaDataUpdateCoordinator.import_statistics"
)


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


async def test_scan_interval_option_applied(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A stored scan interval (seconds) becomes the coordinator's interval."""
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_SCAN_INTERVAL: 3600}
    )

    with patch(_IMPORT_STATS, new=AsyncMock()):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_config_entry.runtime_data.update_interval == timedelta(seconds=3600)


async def test_title_upgraded_from_customer_number(
    hass: HomeAssistant, mock_client
) -> None:
    """A legacy entry titled by customer number is renamed to the account name."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Polenergia ({CUSTOMER_NUMBER})",
        unique_id=f"{USERNAME}_{CUSTOMER_NUMBER}",
        data={
            CONF_USERNAME: USERNAME,
            CONF_PASSWORD: PASSWORD,
            CONF_CUSTOMER_NUMBER: CUSTOMER_NUMBER,
            CONF_ACCOUNT_NAME: ACCOUNT_NAME,
        },
    )
    entry.add_to_hass(hass)

    with patch(_IMPORT_STATS, new=AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.title == f"Polenergia ({ACCOUNT_NAME})"


async def test_legacy_statistics_entities_removed(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Pre-1.5 statistics-only entities are purged from the registry on setup."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        "sensor", DOMAIN, "mp1_statistics", config_entry=mock_config_entry
    )
    assert registry.async_get(legacy.entity_id) is not None

    with patch(_IMPORT_STATS, new=AsyncMock()):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    assert registry.async_get(legacy.entity_id) is None


async def test_clear_statistics_includes_legacy_entities(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Legacy entity-keyed statistics are cleared alongside the external streams."""
    mock_config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    # Registered against no config entry, so setup's purge leaves it in place.
    legacy = registry.async_get_or_create("sensor", DOMAIN, "old_cost_statistics")

    with patch(_IMPORT_STATS, new=AsyncMock()):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    with patch("custom_components.my_polenergia.get_instance") as get_instance:
        await hass.services.async_call(DOMAIN, "clear_statistics", {}, blocking=True)

    cleared = get_instance.return_value.async_clear_statistics.call_args.args[0]
    assert legacy.entity_id in cleared


async def test_services_skip_unloaded_and_dataless_entries(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """An entry whose coordinator has no data yet is quietly skipped."""
    mock_config_entry.add_to_hass(hass)
    with patch(_IMPORT_STATS, new=AsyncMock()):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    mock_config_entry.runtime_data.data = None

    with patch(_IMPORT_STATS, new=AsyncMock()) as import_mock:
        await hass.services.async_call(DOMAIN, "reload_statistics", {}, blocking=True)
    import_mock.assert_not_awaited()


async def test_no_password_starts_reauth(
    hass: HomeAssistant, mock_client
) -> None:
    """An entry with no stored password cannot authenticate and asks for one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{USERNAME}_{CUSTOMER_NUMBER}",
        data={
            CONF_USERNAME: USERNAME,
            CONF_CUSTOMER_NUMBER: CUSTOMER_NUMBER,
        },
    )
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )


async def test_setup_auth_error_starts_reauth(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """An authorization error raised at setup also triggers reauth."""
    mock_client.authenticate.side_effect = PolEnergiaAuthorizationError("nope")
    mock_config_entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_api_error_during_fetch_fails_update(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """An API error becomes UpdateFailed, not a crash."""
    mock_config_entry.add_to_hass(hass)
    mock_client.get_all_data.side_effect = PolEnergiaAPIError("500")

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_connection_error_during_fetch_fails_update(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A connection error becomes UpdateFailed so HA retries."""
    mock_config_entry.add_to_hass(hass)
    mock_client.get_all_data.side_effect = PolEnergiaConnectionError("down")

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_unexpected_error_during_fetch_fails_update(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Anything unexpected is still reported as UpdateFailed."""
    mock_config_entry.add_to_hass(hass)
    mock_client.get_all_data.side_effect = RuntimeError("boom")

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_reauth_connection_error_during_fetch(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A connection failure while re-logging in is retryable, not a reauth flow."""
    mock_config_entry.add_to_hass(hass)
    mock_client.get_all_data.side_effect = PolEnergiaAuthorizationError("expired")
    mock_client.authenticate.side_effect = PolEnergiaConnectionError("down")

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_reauth_credentials_rejected_raises_auth_failed(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Credentials rejected during the re-login start the reauth flow."""
    mock_config_entry.add_to_hass(hass)
    mock_client.get_all_data.side_effect = PolEnergiaAuthorizationError("expired")
    mock_client.authenticate.side_effect = PolEnergiaAuthorizationError("bad password")

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_refetch_after_reauth_propagates_fetch_errors(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A failure on the post-reauth refetch is an update failure."""
    mock_config_entry.add_to_hass(hass)
    mock_client.get_all_data.side_effect = [
        PolEnergiaAuthorizationError("expired"),
        PolEnergiaAPIError("500"),
    ]

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_statistics_failure_does_not_break_refresh(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A recorder failure leaves the sensor data intact."""
    mock_config_entry.add_to_hass(hass)
    fresh = make_data()
    mock_client.get_all_data.return_value = fresh

    coord = _make_coordinator(hass, mock_client, mock_config_entry)
    coord.import_statistics = AsyncMock(side_effect=RuntimeError("recorder down"))

    result = await coord._async_update_data()

    assert result["data"] is fresh


def _full_rebuild_calls(import_stats: AsyncMock) -> list:
    """Every import_statistics call that asked for a from-scratch rebuild."""
    return [c for c in import_stats.call_args_list if c.kwargs.get("full_rebuild")]


async def test_price_change_rebuilds_cost_statistics(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A new import price recomputes stored months, not just future ones."""
    mock_config_entry.add_to_hass(hass)

    with patch(_IMPORT_STATS, new=AsyncMock()) as import_stats:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        assert not _full_rebuild_calls(import_stats)

        hass.config_entries.async_update_entry(
            mock_config_entry, options={CONF_IMPORT_PRICE: 1.23}
        )
        await hass.async_block_till_done()

        assert len(_full_rebuild_calls(import_stats)) == 1
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_per_zone_price_change_rebuilds_cost_statistics(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A per-zone rate counts as a price change too."""
    mock_config_entry.add_to_hass(hass)

    with patch(_IMPORT_STATS, new=AsyncMock()) as import_stats:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        hass.config_entries.async_update_entry(
            mock_config_entry, options={"import_price_z2": 0.55}
        )
        await hass.async_block_till_done()

        assert len(_full_rebuild_calls(import_stats)) == 1


async def test_non_price_option_change_does_not_rebuild(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Changing the scan interval must not re-price history."""
    mock_config_entry.add_to_hass(hass)

    with patch(_IMPORT_STATS, new=AsyncMock()) as import_stats:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        hass.config_entries.async_update_entry(
            mock_config_entry, options={CONF_SCAN_INTERVAL: 3600}
        )
        await hass.async_block_till_done()

        assert not _full_rebuild_calls(import_stats)


async def test_storing_the_default_price_does_not_rebuild(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Saving the placeholder rate unchanged is not a price change."""
    mock_config_entry.add_to_hass(hass)

    with patch(_IMPORT_STATS, new=AsyncMock()) as import_stats:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        hass.config_entries.async_update_entry(
            mock_config_entry, options={CONF_IMPORT_PRICE: DEFAULT_IMPORT_PRICE}
        )
        await hass.async_block_till_done()

        assert not _full_rebuild_calls(import_stats)


async def test_failed_cost_rebuild_still_reloads_entry(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A recorder failure during the rebuild must not break the options flow."""
    mock_config_entry.add_to_hass(hass)

    with patch(_IMPORT_STATS, new=AsyncMock()) as import_stats:
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

        import_stats.side_effect = RuntimeError("recorder gone")
        hass.config_entries.async_update_entry(
            mock_config_entry, options={CONF_IMPORT_PRICE: 1.23}
        )
        await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
