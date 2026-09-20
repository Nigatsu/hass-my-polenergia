"""Device lifecycle (stale pruning, manual removal) and repair issues."""

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from custom_components.my_polenergia import async_remove_config_entry_device
from custom_components.my_polenergia.const import (
    CONF_IMPORT_PRICE,
    DOMAIN,
    ISSUE_IMPORT_PRICE_UNSET,
    ISSUE_NO_READINGS,
)

from .conftest import make_data, make_measurement_point, make_reading

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


async def _refresh(hass: HomeAssistant, entry) -> None:
    """Trigger a coordinator refresh without touching the recorder."""
    with patch(_IMPORT_STATS, new=AsyncMock()):
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()


def _device_by_ppe(hass: HomeAssistant, entry, ppe: str):
    """The device carrying a given PPE identifier on this entry.

    Walks the entry's devices rather than calling ``async_get_device``, which is
    deprecated from HA 2026.9 in favour of ``async_get_device_by_identifier``;
    this way the test works on every version the integration supports.
    """
    registry = dr.async_get(hass)
    return next(
        (
            device
            for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
            if (DOMAIN, ppe) in device.identifiers
        ),
        None,
    )


def _device_ppes(hass: HomeAssistant, entry) -> set[str]:
    """PPEs of the devices currently attached to the entry."""
    registry = dr.async_get(hass)
    return {
        identifier
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
        for domain, identifier in device.identifiers
        if domain == DOMAIN
    }


async def test_stale_device_detached_on_refresh(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A meter removed from the account loses its device on the next refresh."""
    mp1 = make_measurement_point("mp1", ppe="PL0001", address="Main St 1")
    mp2 = make_measurement_point("mp2", ppe="PL0002", address="Side St 2")
    mock_client.get_all_data.return_value = make_data([mp1, mp2])

    await _setup(hass, mock_config_entry)
    assert _device_ppes(hass, mock_config_entry) == {"PL0001", "PL0002"}

    mock_client.get_all_data.return_value = make_data([mp1])
    await _refresh(hass, mock_config_entry)

    assert _device_ppes(hass, mock_config_entry) == {"PL0001"}


async def test_empty_payload_does_not_prune_devices(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A refresh that returns no meters is a fetch problem, not a removal."""
    mp1 = make_measurement_point("mp1", ppe="PL0001")
    mock_client.get_all_data.return_value = make_data([mp1])

    await _setup(hass, mock_config_entry)
    mock_client.get_all_data.return_value = make_data([])
    await _refresh(hass, mock_config_entry)

    assert _device_ppes(hass, mock_config_entry) == {"PL0001"}


async def test_manual_device_removal_allowed_only_for_gone_meters(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """Deleting a live meter's device by hand is refused; a gone one is allowed."""
    mp1 = make_measurement_point("mp1", ppe="PL0001")
    mock_client.get_all_data.return_value = make_data([mp1])
    await _setup(hass, mock_config_entry)

    live = _device_by_ppe(hass, mock_config_entry, "PL0001")
    assert live is not None
    assert not await async_remove_config_entry_device(hass, mock_config_entry, live)

    registry = dr.async_get(hass)
    orphan = registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, "PL9999")},
    )
    assert await async_remove_config_entry_device(hass, mock_config_entry, orphan)


async def test_price_repair_issue_raised_and_cleared(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """The placeholder price raises a repair issue that clears once set."""
    await _setup(hass, mock_config_entry)

    issue_id = f"{ISSUE_IMPORT_PRICE_UNSET}_{mock_config_entry.entry_id}"
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    hass.config_entries.async_update_entry(
        mock_config_entry, options={CONF_IMPORT_PRICE: 1.10}
    )
    await hass.async_block_till_done()
    await _refresh(hass, mock_config_entry)

    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_no_readings_repair_issue_raised_and_cleared(
    hass: HomeAssistant, mock_client, mock_config_entry
) -> None:
    """A meter that yields no readings gets its own actionable issue."""
    mp1 = make_measurement_point("mp1", ppe="PL0001")
    mock_client.get_all_data.return_value = make_data([mp1])

    await _setup(hass, mock_config_entry)

    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"{ISSUE_NO_READINGS}_mp1") is not None

    mock_client.get_all_data.return_value = make_data(
        [mp1], {"mp1": [make_reading(2024, 1, 100.0, "mp1")]}
    )
    await _refresh(hass, mock_config_entry)

    assert registry.async_get_issue(DOMAIN, f"{ISSUE_NO_READINGS}_mp1") is None
