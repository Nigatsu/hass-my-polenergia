"""Config flow for My PolEnergia integration."""

from datetime import datetime
import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
import voluptuous as vol

from .const import (
    CONF_ACCOUNT_NAME,
    CONF_CUSTOMER_NUMBER,
    CONF_IMPORT_PRICE,
    CONF_ZONE_COUNT,
    DEFAULT_IMPORT_PRICE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    ZONE_COUNT_AUTO,
    import_price_key,
)
from .polenergia.client import PolEnergiaClient
from .polenergia.errors import (
    PolEnergiaAuthorizationError,
    PolEnergiaConnectionError,
    PolEnergiaError,
)
from .polenergia.tariffs import zone_count, zone_display_name, zone_keys_for_count

_LOGGER = logging.getLogger(__name__)

CONF_FROM_DATE = "from_date"


class PolEnergiaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for My PolEnergia."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise the per-flow state carried between steps."""
        self._username: str | None = None
        self._password: str | None = None
        self._account_name: str | None = None
        self._customer_numbers: list[str] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}

        if user_input is not None:
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]

            try:
                client = PolEnergiaClient(session=async_create_clientsession(self.hass))
                authenticated = await client.authenticate(self._username, self._password)

                if not authenticated:
                    errors["base"] = "invalid_auth"
                elif not client.connector.access_token:
                    errors["base"] = "no_access_token"
                else:
                    self._customer_numbers = await client.get_customer_numbers()

                    if not self._customer_numbers:
                        errors["base"] = "no_customer_numbers"
                    elif len(self._customer_numbers) == 1:
                        customer_number = self._customer_numbers[0]
                        self._account_name = await client.get_account_name(customer_number)
                        return await self._create_entry(customer_number)
                    else:
                        return await self.async_step_customer_number()

            except AbortFlow:
                # e.g. account already configured — must propagate, not be
                # swallowed by the broad handler below.
                raise
            except PolEnergiaAuthorizationError:
                errors["base"] = "invalid_auth"
            except PolEnergiaConnectionError:
                errors["base"] = "cannot_connect"
            except PolEnergiaError:
                errors["base"] = "unknown"
            except Exception:
                _LOGGER.exception("Unexpected exception during authentication")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }),
            errors=errors,
        )

    async def async_step_customer_number(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            customer_number = user_input[CONF_CUSTOMER_NUMBER]
            return await self._create_entry(customer_number)

        return self.async_show_form(
            step_id="customer_number",
            data_schema=vol.Schema({
                vol.Required(CONF_CUSTOMER_NUMBER): vol.In(
                    {num: num for num in (self._customer_numbers or [])}
                ),
            }),
        )

    async def _create_entry(self, customer_number: str) -> ConfigFlowResult:
        await self.async_set_unique_id(f"{self._username}_{customer_number}")
        self._abort_if_unique_id_configured()

        title = f"Polenergia ({self._account_name})" if self._account_name else f"Polenergia ({customer_number})"

        return self.async_create_entry(
            title=title,
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_CUSTOMER_NUMBER: customer_number,
                CONF_ACCOUNT_NAME: self._account_name,
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start the reauth flow after the stored password stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again and re-verify it."""
        return await self._async_step_password(
            self._get_reauth_entry(), "reauth_confirm", user_input
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user update the stored password from the entry menu.

        Entry data is credentials only, so the password is the whole of what
        there is to reconfigure; everything else lives in options.
        """
        return await self._async_step_password(
            self._get_reconfigure_entry(), "reconfigure", user_input
        )

    async def _async_step_password(
        self,
        entry: config_entries.ConfigEntry,
        step_id: str,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Shared password-re-entry step behind reauth and reconfigure."""
        errors: dict[str, str] = {}
        username = entry.data[CONF_USERNAME]

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            customer_number = entry.data[CONF_CUSTOMER_NUMBER]

            try:
                client = PolEnergiaClient(session=async_create_clientsession(self.hass))
                authenticated = await client.authenticate(username, password)

                if not authenticated:
                    errors["base"] = "invalid_auth"
                else:
                    account_name = await client.get_account_name(customer_number)
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_PASSWORD: password,
                            CONF_ACCOUNT_NAME: account_name
                            or entry.data.get(CONF_ACCOUNT_NAME),
                        },
                    )

            except PolEnergiaAuthorizationError:
                errors["base"] = "invalid_auth"
            except PolEnergiaConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during re-authentication")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={"username": username},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        return PolEnergiaOptionsFlow()


class PolEnergiaOptionsFlow(config_entries.OptionsFlow):
    """Multi-step options menu for My PolEnergia."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "set_price",
                "scan_interval",
                "reload_history",
                "clear_stats",
            ],
        )

    def _detected_zone_keys(self) -> list[str]:
        """Zone slugs to show price fields for.

        Precedence: an explicit user override, then the zones actually present in
        the readings, then the zone count implied by the detected tariff group.
        Single-zone accounts get exactly the one field they have today.
        """
        override = self.config_entry.options.get(CONF_ZONE_COUNT, ZONE_COUNT_AUTO)
        if override != ZONE_COUNT_AUTO:
            try:
                return zone_keys_for_count(int(override))
            except (TypeError, ValueError):
                pass

        # runtime_data is only populated while the entry is loaded; fall back to
        # whatever prices are already stored if it is not.
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if coordinator is not None:
            seen = {zone for zones in coordinator.zones_seen.values() for zone in zones}
            if seen:
                return sorted(seen)
            data = (coordinator.data or {}).get("data")
            if data is not None:
                counts = [zone_count(mp.tariff) for mp in data.measurement_points if mp.tariff]
                if counts:
                    return zone_keys_for_count(max(counts))

        stored = [
            zone
            for zone in ("z1", "z2", "z3")
            if self.config_entry.options.get(import_price_key(zone)) is not None
        ]
        return zone_keys_for_count(len(stored) + 1) if stored else []

    async def async_step_set_price(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        zone_keys = self._detected_zone_keys()

        if user_input is not None:
            new_options = {**self.config_entry.options, **user_input}
            return self.async_create_entry(title="", data=new_options)

        options = self.config_entry.options
        price = vol.All(vol.Coerce(float), vol.Range(min=0.0))
        schema: dict[Any, Any] = {}

        if zone_keys:
            # Zone 1 reuses CONF_IMPORT_PRICE so an existing single-rate setting
            # carries over instead of resetting to the placeholder.
            for zone in zone_keys:
                key = import_price_key(zone if zone != "z1" else None)
                default = float(options.get(key, options.get(CONF_IMPORT_PRICE, DEFAULT_IMPORT_PRICE)))
                schema[vol.Required(key, default=default)] = price
        else:
            default = float(options.get(CONF_IMPORT_PRICE, DEFAULT_IMPORT_PRICE))
            schema[vol.Required(CONF_IMPORT_PRICE, default=default)] = price

        current_override = options.get(CONF_ZONE_COUNT, ZONE_COUNT_AUTO)
        schema[vol.Optional(CONF_ZONE_COUNT, default=current_override)] = vol.In(
            [ZONE_COUNT_AUTO, "1", "2", "3"]
        )

        return self.async_show_form(
            step_id="set_price",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "zones": ", ".join(zone_display_name(z) for z in zone_keys) or "—",
            },
        )

    async def async_step_scan_interval(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            new_options = {**self.config_entry.options, **user_input}
            return self.async_create_entry(title="", data=new_options)

        current_interval = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        if hasattr(current_interval, "total_seconds"):
            current_seconds = int(current_interval.total_seconds())
        else:
            current_seconds = int(current_interval)

        return self.async_show_form(
            step_id="scan_interval",
            data_schema=vol.Schema({
                vol.Optional(CONF_SCAN_INTERVAL, default=current_seconds): vol.All(
                    cv.positive_int,
                    vol.Range(min=int(MIN_SCAN_INTERVAL.total_seconds())),
                ),
            }),
        )

    async def async_step_reload_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            from_date = user_input.get(CONF_FROM_DATE, "").strip()
            service_data: dict[str, Any] = {}
            if from_date:
                try:
                    datetime.fromisoformat(from_date)
                    service_data[CONF_FROM_DATE] = from_date
                except ValueError:
                    errors["base"] = "invalid_date"

            if not errors:
                await self.hass.services.async_call(
                    DOMAIN, "reload_statistics", service_data, blocking=False
                )
                return self.async_create_entry(title="", data=self.config_entry.options)

        return self.async_show_form(
            step_id="reload_history",
            data_schema=vol.Schema({
                vol.Optional(CONF_FROM_DATE, default=""): str,
            }),
            errors=errors,
        )

    async def async_step_clear_stats(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            if user_input.get("confirm"):
                await self.hass.services.async_call(
                    DOMAIN, "clear_statistics", {}, blocking=False
                )
            return self.async_create_entry(title="", data=self.config_entry.options)

        return self.async_show_form(
            step_id="clear_stats",
            data_schema=vol.Schema({
                vol.Required("confirm", default=False): bool,
            }),
        )
