"""Data update coordinator for PolEnergia integration."""

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder.models import StatisticData
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..polenergia.client import PolEnergiaClient
from ..polenergia.errors import (
    PolEnergiaAPIError,
    PolEnergiaAuthorizationError,
    PolEnergiaConnectionError,
)

_LOGGER = logging.getLogger(__name__)


class PolEnergiaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching PolEnergia data from the API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: PolEnergiaClient,
        customer_number: str,
        update_interval: timedelta,
        config_entry: ConfigEntry,
    ):
        self.client = client
        self.customer_number = customer_number

        super().__init__(
            hass,
            _LOGGER,
            name="Polenergia",
            update_interval=update_interval,
            config_entry=config_entry,
        )

    async def _async_setup(self) -> None:
        """One-time authentication before the first refresh.

        ``DataUpdateCoordinator`` calls this once. Raising ``ConfigEntryAuthFailed``
        starts the reauth flow; raising ``UpdateFailed`` here is converted by HA into
        ``ConfigEntryNotReady`` (retried later).
        """
        username = self.config_entry.data[CONF_USERNAME]
        password = self.config_entry.data.get(CONF_PASSWORD)

        if not password:
            raise ConfigEntryAuthFailed(
                "No password available. Please reconfigure the integration."
            )

        try:
            # Token kept in client memory only — re-auth on each HA restart.
            authenticated = await self.client.authenticate(username, password)
            if not authenticated:
                raise ConfigEntryAuthFailed(
                    "Authentication failed. Please check your credentials."
                )
            _LOGGER.info("Authenticated %s", username)
        except PolEnergiaAuthorizationError as err:
            raise ConfigEntryAuthFailed(
                "Authentication failed. Please check your credentials."
            ) from err
        except PolEnergiaConnectionError as err:
            raise UpdateFailed(f"Connection failed: {err}") from err

    async def _async_update_data(self) -> dict:
        """Fetch data from API endpoint."""
        try:
            data = await self.client.get_all_data(customer_number=self.customer_number)

            return {
                "data": data,
                "statistics": self.data.get("statistics", {}) if self.data else {},
            }

        except PolEnergiaAuthorizationError as err:
            # Token expiry is expected (token lives in memory only). Try a single
            # silent re-login. If credentials are now invalid, surface a reauth
            # flow rather than failing forever.
            _LOGGER.warning("Token expired during data fetch — attempting re-authentication")

            username = self.config_entry.data[CONF_USERNAME]
            password = self.config_entry.data[CONF_PASSWORD]

            try:
                authenticated = await self.client.authenticate(username, password)
            except PolEnergiaAuthorizationError as reauth_err:
                raise ConfigEntryAuthFailed("Re-authentication failed") from reauth_err
            except PolEnergiaConnectionError as conn_err:
                raise UpdateFailed(f"Connection failed during re-auth: {conn_err}") from conn_err

            if not authenticated:
                raise ConfigEntryAuthFailed("Re-authentication failed") from err

            _LOGGER.info("Re-authenticated successfully")

            try:
                data = await self.client.get_all_data(customer_number=self.customer_number)
            except PolEnergiaConnectionError as conn_err:
                raise UpdateFailed(f"Connection failed: {conn_err}") from conn_err
            except PolEnergiaAPIError as api_err:
                raise UpdateFailed(f"API error: {api_err}") from api_err

            return {
                "data": data,
                "statistics": self.data.get("statistics", {}) if self.data else {},
            }

        except PolEnergiaConnectionError as err:
            raise UpdateFailed(f"Connection failed: {err}") from err

        except PolEnergiaAPIError as err:
            raise UpdateFailed(f"API error: {err}") from err

        except Exception as err:
            _LOGGER.exception("Unexpected error fetching data")
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def fetch_historical_statistics(
        self,
        measurement_point_id: str,
        from_date: datetime,
        price: float = 0.0,
    ) -> dict[str, list[StatisticData]]:
        """Fetch historical readings and convert to HA statistics streams.

        Returns dict with keys:
            "energy": cumulative kWh statistics
            "cost":   cumulative PLN statistics (kWh × price)
        """
        try:
            readings = await self.client.get_readings(
                from_date=from_date,
                to_date=None,  # let client default to tomorrow 00:00 UTC
            )

            if not readings:
                _LOGGER.warning("No historical readings found for %s", measurement_point_id)
                return {"energy": [], "cost": []}

            readings.sort(key=lambda r: r.period_anchor)

            energy_stats: list[StatisticData] = []
            cost_stats: list[StatisticData] = []
            cumulative_energy = 0.0
            cumulative_cost = 0.0

            for reading in readings:
                cumulative_energy += reading.value
                cumulative_cost += reading.value * price
                energy_stats.append(StatisticData(
                    start=reading.period_anchor,
                    state=reading.value,
                    sum=cumulative_energy,
                ))
                cost_stats.append(StatisticData(
                    start=reading.period_anchor,
                    state=reading.value * price,
                    sum=cumulative_cost,
                ))

            # Anchor at start of current month with zero so graph doesn't
            # extrapolate forward from last real data point (end of prev month).
            now = datetime.now(tz=timezone.utc)
            current_month_start = datetime(now.year, now.month, 1, 0, 0, 0, tzinfo=timezone.utc)
            if energy_stats and energy_stats[-1]["start"] < current_month_start:
                energy_stats.append(StatisticData(
                    start=current_month_start,
                    state=0.0,
                    sum=cumulative_energy,
                ))
                cost_stats.append(StatisticData(
                    start=current_month_start,
                    state=0.0,
                    sum=cumulative_cost,
                ))

            _LOGGER.info(
                "Converted %d monthly readings for %s (total: %.1f kWh, %.2f PLN @ %.4f PLN/kWh)",
                len(energy_stats),
                measurement_point_id,
                cumulative_energy,
                cumulative_cost,
                price,
            )
            return {"energy": energy_stats, "cost": cost_stats}

        except Exception as err:
            _LOGGER.error("Failed to fetch historical statistics for %s: %s", measurement_point_id, err)
            return {"energy": [], "cost": []}
