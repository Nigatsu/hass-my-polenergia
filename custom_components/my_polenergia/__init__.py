"""My PolEnergia Home Assistant integration."""

import logging
from datetime import datetime, timedelta

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_NAME,
    CONF_CUSTOMER_NUMBER,
    CONF_HISTORICAL_IMPORT_DONE,
    CONF_IMPORT_PRICE,
    CONF_TOKEN_EXPIRY,
    DEFAULT_IMPORT_PRICE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .hass_integration.coordinator import PolEnergiaDataUpdateCoordinator
from .polenergia.client import PolEnergiaClient
from .polenergia.errors import PolEnergiaAuthorizationError, PolEnergiaConnectionError

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]

SERVICE_RELOAD_STATISTICS = "reload_statistics"
SERVICE_CLEAR_STATISTICS = "clear_statistics"
CONF_FROM_DATE = "from_date"

RELOAD_STATISTICS_SCHEMA = vol.Schema({
    vol.Optional(CONF_FROM_DATE): cv.string,
})

CLEAR_STATISTICS_SCHEMA = vol.Schema({})


def _get_price(entry: ConfigEntry) -> float:
    return float(entry.options.get(CONF_IMPORT_PRICE, DEFAULT_IMPORT_PRICE))


async def _rebuild_statistics(
    hass: HomeAssistant,
    cfg_entry: ConfigEntry,
    from_date: datetime | None = None,
) -> None:
    """Re-fetch readings and push fresh energy + cost statistics to recorder."""
    entry_data = hass.data[DOMAIN].get(cfg_entry.entry_id)
    if not entry_data:
        return

    coord: PolEnergiaDataUpdateCoordinator = entry_data["coordinator"]
    cli: PolEnergiaClient = entry_data["client"]
    cust_num = cfg_entry.data[CONF_CUSTOMER_NUMBER]
    price = _get_price(cfg_entry)

    if from_date is None:
        from_date = await cli.get_earliest_agreement_date(cust_num)
        if not from_date:
            from_date = datetime.now() - timedelta(days=730)
            _LOGGER.warning("No agreement date for %s — using 2-year fallback", cfg_entry.title)

    if not (coord.data and coord.data.get("data")):
        return

    data = coord.data["data"]
    statistics_dict: dict[str, dict] = {}
    for mp in data.measurement_points:
        streams = await coord.fetch_historical_statistics(
            measurement_point_id=mp.id,
            from_date=from_date,
            price=price,
        )
        if streams.get("energy") or streams.get("cost"):
            statistics_dict[mp.id] = streams

    coord.data["statistics"] = statistics_dict
    coord.async_set_updated_data(coord.data)
    _LOGGER.info("Statistics rebuilt for %s (price=%.4f PLN/kWh)", cfg_entry.title, price)

    new_options = cfg_entry.options.copy()
    new_options[CONF_HISTORICAL_IMPORT_DONE] = True
    hass.config_entries.async_update_entry(cfg_entry, options=new_options)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PolEnergia from a config entry."""
    username = entry.data[CONF_USERNAME]
    password = entry.data.get(CONF_PASSWORD)
    access_token = entry.data.get(CONF_ACCESS_TOKEN)
    token_expiry_str = entry.data.get(CONF_TOKEN_EXPIRY)
    customer_number = entry.data[CONF_CUSTOMER_NUMBER]

    if not password:
        raise ConfigEntryAuthFailed("No password available. Please reconfigure the integration.")

    scan_interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    if isinstance(scan_interval, int):
        scan_interval = timedelta(seconds=scan_interval)

    client = PolEnergiaClient()

    try:
        token_valid = False
        if access_token and token_expiry_str:
            try:
                token_expiry = datetime.fromisoformat(token_expiry_str)
                if datetime.now() < token_expiry - timedelta(minutes=5):
                    token_valid = True
                    _LOGGER.debug("Using stored access token (expires: %s)", token_expiry)
            except (ValueError, TypeError) as err:
                _LOGGER.warning("Could not parse token expiry: %s", err)

        if token_valid:
            client.connector._access_token = access_token
            client.connector._token_expiry = datetime.fromisoformat(token_expiry_str)
        else:
            _LOGGER.info("Token expired — re-authenticating for %s", username)
            authenticated = await client.authenticate(username, password)
            if not authenticated:
                await client.close()
                raise ConfigEntryAuthFailed("Authentication failed. Please check your credentials.")

            new_data = entry.data.copy()
            new_data[CONF_ACCESS_TOKEN] = client.connector._access_token
            new_data[CONF_TOKEN_EXPIRY] = client.connector._token_expiry.isoformat() if client.connector._token_expiry else None
            hass.config_entries.async_update_entry(entry, data=new_data)
            _LOGGER.info("Re-authenticated and stored new token for %s", username)

    except PolEnergiaAuthorizationError as err:
        await client.close()
        raise ConfigEntryAuthFailed("Authentication failed. Please check your credentials.") from err

    except PolEnergiaConnectionError as err:
        await client.close()
        raise ConfigEntryNotReady(f"Connection failed: {err}") from err

    except Exception as err:
        await client.close()
        raise ConfigEntryNotReady(f"Setup failed: {err}") from err

    coordinator = PolEnergiaDataUpdateCoordinator(
        hass=hass,
        client=client,
        customer_number=customer_number,
        update_interval=scan_interval,
        config_entry=entry,
    )

    await coordinator.async_config_entry_first_refresh()

    account_name = entry.data.get(CONF_ACCOUNT_NAME)
    if account_name and entry.title == f"Polenergia ({customer_number})":
        hass.config_entries.async_update_entry(entry, title=f"Polenergia ({account_name})")

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator, "client": client}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Import historical statistics on first setup (after platforms so sensors
    # receive the coordinator update event and import into recorder).
    if not entry.options.get(CONF_HISTORICAL_IMPORT_DONE, False):
        _LOGGER.info("First setup — importing historical statistics")
        hass.async_create_task(_rebuild_statistics(hass, entry))

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    _register_services(hass)

    return True


def _register_services(hass: HomeAssistant) -> None:
    """Register domain-wide services (idempotent)."""

    if not hass.services.has_service(DOMAIN, SERVICE_RELOAD_STATISTICS):
        async def handle_reload_statistics(call: ServiceCall) -> None:
            from_date_str = call.data.get(CONF_FROM_DATE)
            custom_from_date = None
            if from_date_str:
                try:
                    custom_from_date = datetime.fromisoformat(from_date_str)
                except ValueError as err:
                    _LOGGER.error("Invalid from_date format: %s — %s", from_date_str, err)
                    return

            for cfg_entry in hass.config_entries.async_entries(DOMAIN):
                try:
                    await _rebuild_statistics(hass, cfg_entry, from_date=custom_from_date)
                except Exception as err:
                    _LOGGER.error("Failed to reload statistics for %s: %s", cfg_entry.title, err)

        hass.services.async_register(
            DOMAIN, SERVICE_RELOAD_STATISTICS, handle_reload_statistics, schema=RELOAD_STATISTICS_SCHEMA
        )

    if not hass.services.has_service(DOMAIN, SERVICE_CLEAR_STATISTICS):
        async def handle_clear_statistics(call: ServiceCall) -> None:
            statistic_ids: list[str] = []
            for cfg_entry in hass.config_entries.async_entries(DOMAIN):
                entry_data = hass.data[DOMAIN].get(cfg_entry.entry_id)
                if not entry_data:
                    continue
                coord = entry_data["coordinator"]
                if not (coord.data and coord.data.get("data")):
                    continue
                data = coord.data["data"]
                for mp in data.measurement_points:
                    # entity_ids derived from device name slugification; can't
                    # reconstruct reliably here — collect from entity registry.
                    pass
                # Reset coordinator's cached statistics so importers re-run.
                coord.data["statistics"] = {}
                coord.async_set_updated_data(coord.data)

                new_options = cfg_entry.options.copy()
                new_options[CONF_HISTORICAL_IMPORT_DONE] = False
                hass.config_entries.async_update_entry(cfg_entry, options=new_options)

            # Resolve statistic_ids via entity registry: all my_polenergia
            # entities with unique_id ending in "_statistics" or "_cost_statistics".
            from homeassistant.helpers import entity_registry as er
            registry = er.async_get(hass)
            for entry_obj in registry.entities.values():
                if entry_obj.platform != DOMAIN:
                    continue
                if entry_obj.unique_id.endswith("_statistics") or entry_obj.unique_id.endswith("_cost_statistics"):
                    statistic_ids.append(entry_obj.entity_id)

            if statistic_ids:
                from homeassistant.components.recorder import get_instance
                get_instance(hass).async_clear_statistics(statistic_ids)
                _LOGGER.info("Cleared statistics for %d entities", len(statistic_ids))
            else:
                _LOGGER.warning("clear_statistics called but no matching entities found")

        hass.services.async_register(
            DOMAIN, SERVICE_CLEAR_STATISTICS, handle_clear_statistics, schema=CLEAR_STATISTICS_SCHEMA
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})
        client = entry_data.get("client")
        if client:
            await client.close()
    return unload_ok


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update — reload entry so new scan_interval / price take effect."""
    await hass.config_entries.async_reload(entry.entry_id)
