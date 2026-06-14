"""My PolEnergia Home Assistant integration."""

from datetime import UTC, datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol

from .const import (
    CONF_ACCOUNT_NAME,
    CONF_CUSTOMER_NUMBER,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .hass_integration.coordinator import PolEnergiaDataUpdateCoordinator
from .polenergia.client import PolEnergiaClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

type PolEnergiaConfigEntry = ConfigEntry[PolEnergiaDataUpdateCoordinator]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SERVICE_RELOAD_STATISTICS = "reload_statistics"
SERVICE_CLEAR_STATISTICS = "clear_statistics"
CONF_FROM_DATE = "from_date"

# Unique-id suffixes of the obsolete statistics-only sensors (pre-Phase-2).
_LEGACY_STAT_SUFFIXES = ("_statistics", "_cost_statistics")

RELOAD_STATISTICS_SCHEMA = vol.Schema({
    vol.Optional(CONF_FROM_DATE): cv.string,
})

CLEAR_STATISTICS_SCHEMA = vol.Schema({})


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain-wide services once, independent of any config entry."""
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PolEnergiaConfigEntry) -> bool:
    """Set up PolEnergia from a config entry."""
    customer_number = entry.data[CONF_CUSTOMER_NUMBER]

    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    if isinstance(scan_interval, int):
        scan_interval = timedelta(seconds=scan_interval)

    # HA-managed, integration-owned session (own cookie jar for the login flow).
    client = PolEnergiaClient(session=async_create_clientsession(hass))

    coordinator = PolEnergiaDataUpdateCoordinator(
        hass=hass,
        client=client,
        customer_number=customer_number,
        update_interval=scan_interval,
        config_entry=entry,
    )

    # Authentication happens in coordinator._async_setup; the first refresh also
    # imports historical statistics into the recorder (see import_statistics).
    await coordinator.async_config_entry_first_refresh()

    account_name = entry.data.get(CONF_ACCOUNT_NAME)
    if account_name and entry.title == f"Polenergia ({customer_number})":
        hass.config_entries.async_update_entry(entry, title=f"Polenergia ({account_name})")

    entry.runtime_data = coordinator

    _remove_legacy_stat_entities(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    return True


def _remove_legacy_stat_entities(hass: HomeAssistant, entry: PolEnergiaConfigEntry) -> None:
    """Drop the obsolete statistics-only sensor entities from the registry.

    Their external statistics streams are untouched — only the dummy entities go.
    """
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.unique_id.endswith(_LEGACY_STAT_SUFFIXES):
            _LOGGER.info("Removing obsolete statistics entity %s", reg_entry.entity_id)
            registry.async_remove(reg_entry.entity_id)


def _register_services(hass: HomeAssistant) -> None:
    """Register the reload/clear statistics services."""

    async def handle_reload_statistics(call: ServiceCall) -> None:
        from_date_str = call.data.get(CONF_FROM_DATE)
        from_date: datetime | None = None
        if from_date_str:
            try:
                from_date = datetime.fromisoformat(from_date_str)
            except ValueError as err:
                _LOGGER.error("Invalid from_date format: %s — %s", from_date_str, err)
                return
            if from_date.tzinfo is None:
                from_date = from_date.replace(tzinfo=UTC)

        for entry in _loaded_entries(hass):
            coord = entry.runtime_data
            if not (coord.data and coord.data.get("data")):
                continue
            try:
                await coord.import_statistics(
                    coord.data["data"], from_date=from_date, full_rebuild=True
                )
                _LOGGER.info("Reloaded statistics for %s", entry.title)
            except Exception as err:
                _LOGGER.error("Failed to reload statistics for %s: %s", entry.title, err)

    async def handle_clear_statistics(call: ServiceCall) -> None:
        statistic_ids: list[str] = []
        for entry in _loaded_entries(hass):
            coord = entry.runtime_data
            if coord.data and coord.data.get("data"):
                for mp in coord.data["data"].measurement_points:
                    statistic_ids.append(f"{DOMAIN}:{mp.id}_energy")
                    statistic_ids.append(f"{DOMAIN}:{mp.id}_cost")

        # Also wipe legacy entity_id-keyed stats from much older versions.
        registry = er.async_get(hass)
        for reg_entry in registry.entities.values():
            if reg_entry.platform != DOMAIN:
                continue
            if reg_entry.unique_id.endswith(_LEGACY_STAT_SUFFIXES):
                statistic_ids.append(reg_entry.entity_id)

        if statistic_ids:
            from homeassistant.components.recorder import get_instance
            get_instance(hass).async_clear_statistics(statistic_ids)
            _LOGGER.info(
                "Cleared %d statistic streams — call reload_statistics to re-import",
                len(statistic_ids),
            )
        else:
            _LOGGER.warning("clear_statistics called but no matching streams found")

    hass.services.async_register(
        DOMAIN, SERVICE_RELOAD_STATISTICS, handle_reload_statistics, schema=RELOAD_STATISTICS_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_STATISTICS, handle_clear_statistics, schema=CLEAR_STATISTICS_SCHEMA
    )


def _loaded_entries(hass: HomeAssistant) -> list[PolEnergiaConfigEntry]:
    """Loaded config entries for this domain (safe to touch runtime_data)."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]


async def async_unload_entry(hass: HomeAssistant, entry: PolEnergiaConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_update_options(hass: HomeAssistant, entry: PolEnergiaConfigEntry) -> None:
    """Handle options update — reload entry so new scan_interval / price take effect."""
    await hass.config_entries.async_reload(entry.entry_id)
