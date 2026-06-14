"""Config flow for My PolEnergia integration."""

import logging
from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow, FlowResult
from homeassistant.helpers.aiohttp_client import async_create_clientsession
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from .const import (
    CONF_ACCOUNT_NAME,
    CONF_CUSTOMER_NUMBER,
    CONF_IMPORT_PRICE,
    DEFAULT_IMPORT_PRICE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .polenergia.client import PolEnergiaClient
from .polenergia.errors import (
    PolEnergiaAuthorizationError,
    PolEnergiaConnectionError,
    PolEnergiaError,
)

_LOGGER = logging.getLogger(__name__)

CONF_FROM_DATE = "from_date"


class PolEnergiaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for My PolEnergia."""

    VERSION = 1

    def __init__(self):
        self._username: str | None = None
        self._password: str | None = None
        self._account_name: str | None = None
        self._customer_numbers: list[str] | None = None
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
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
    ) -> FlowResult:
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

    async def _create_entry(self, customer_number: str) -> FlowResult:
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

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        self._reauth_entry = self._get_reauth_entry()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors = {}

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            username = self._reauth_entry.data[CONF_USERNAME]
            customer_number = self._reauth_entry.data[CONF_CUSTOMER_NUMBER]

            try:
                client = PolEnergiaClient(session=async_create_clientsession(self.hass))
                authenticated = await client.authenticate(username, password)

                if not authenticated:
                    errors["base"] = "invalid_auth"
                else:
                    account_name = await client.get_account_name(customer_number)
                    new_data = {
                        **self._reauth_entry.data,
                        CONF_PASSWORD: password,
                        CONF_ACCOUNT_NAME: account_name or self._reauth_entry.data.get(CONF_ACCOUNT_NAME),
                    }
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry,
                        data=new_data,
                    )
                    _LOGGER.info("Successfully re-authenticated %s", username)
                    await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                    return self.async_abort(reason="reauth_successful")

            except PolEnergiaAuthorizationError:
                errors["base"] = "invalid_auth"
            except PolEnergiaConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during re-authentication")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={
                "username": self._reauth_entry.data[CONF_USERNAME] if self._reauth_entry else "",
            },
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
    ) -> FlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "set_price",
                "scan_interval",
                "reload_history",
                "clear_stats",
                "change_credentials",
            ],
        )

    async def async_step_set_price(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            new_options = {**self.config_entry.options, **user_input}
            return self.async_create_entry(title="", data=new_options)

        current_price = float(self.config_entry.options.get(CONF_IMPORT_PRICE, DEFAULT_IMPORT_PRICE))

        return self.async_show_form(
            step_id="set_price",
            data_schema=vol.Schema({
                vol.Required(CONF_IMPORT_PRICE, default=current_price): vol.All(
                    vol.Coerce(float),
                    vol.Range(min=0.0),
                ),
            }),
        )

    async def async_step_scan_interval(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
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
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            from_date = user_input.get(CONF_FROM_DATE, "").strip()
            service_data: dict[str, Any] = {}
            if from_date:
                # Validate
                from datetime import datetime as _dt
                try:
                    _dt.fromisoformat(from_date)
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
    ) -> FlowResult:
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

    async def async_step_change_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            username = self.config_entry.data[CONF_USERNAME]
            customer_number = self.config_entry.data[CONF_CUSTOMER_NUMBER]

            try:
                client = PolEnergiaClient(session=async_create_clientsession(self.hass))
                authenticated = await client.authenticate(username, password)
                if not authenticated:
                    errors["base"] = "invalid_auth"
                else:
                    account_name = await client.get_account_name(customer_number)
                    new_data = {
                        **self.config_entry.data,
                        CONF_PASSWORD: password,
                        CONF_ACCOUNT_NAME: account_name or self.config_entry.data.get(CONF_ACCOUNT_NAME),
                    }
                    self.hass.config_entries.async_update_entry(
                        self.config_entry, data=new_data
                    )
                    await self.hass.config_entries.async_reload(self.config_entry.entry_id)
                    return self.async_create_entry(title="", data=self.config_entry.options)

            except PolEnergiaAuthorizationError:
                errors["base"] = "invalid_auth"
            except PolEnergiaConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception during credential update")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="change_credentials",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            description_placeholders={
                "username": self.config_entry.data[CONF_USERNAME],
            },
            errors=errors,
        )
