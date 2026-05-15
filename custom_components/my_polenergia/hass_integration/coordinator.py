"""Data update coordinator for PolEnergia integration."""

import logging
from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder.models import StatisticData
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..const import CONF_ACCESS_TOKEN, CONF_TOKEN_EXPIRY
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
        self.config_entry = config_entry

        super().__init__(
            hass,
            _LOGGER,
            name="Polenergia",
            update_interval=update_interval,
        )

    async def _async_update_data(self) -> dict:
        """Fetch data from API endpoint."""
        try:
            data = await self.client.get_all_data(customer_number=self.customer_number)

            return {
                "data": data,
                "statistics": self.data.get("statistics", {}) if self.data else {},
            }

        except PolEnergiaAuthorizationError as err:
            _LOGGER.warning("Token expired during data fetch — attempting re-authentication")

            try:
                username = self.config_entry.data[CONF_USERNAME]
                password = self.config_entry.data[CONF_PASSWORD]

                authenticated = await self.client.authenticate(username, password)
                if not authenticated:
                    raise UpdateFailed("Re-authentication failed") from err

                new_data = self.config_entry.data.copy()
                new_data[CONF_ACCESS_TOKEN] = self.client.connector._access_token
                new_data[CONF_TOKEN_EXPIRY] = self.client.connector._token_expiry.isoformat() if self.client.connector._token_expiry else None
                self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
                _LOGGER.info("Re-authenticated successfully")

                data = await self.client.get_all_data(customer_number=self.customer_number)
                return {
                    "data": data,
                    "statistics": self.data.get("statistics", {}) if self.data else {},
                }

            except Exception as reauth_err:
                raise UpdateFailed(f"Re-authentication failed: {reauth_err}") from reauth_err

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
                to_date=datetime.now(),
            )

            if not readings:
                _LOGGER.warning("No historical readings found for %s", measurement_point_id)
                return {"energy": [], "cost": []}

            readings.sort(key=lambda r: r.period_start)

            energy_stats: list[StatisticData] = []
            cost_stats: list[StatisticData] = []
            cumulative_energy = 0.0
            cumulative_cost = 0.0

            for reading in readings:
                cumulative_energy += reading.value
                cumulative_cost += reading.value * price
                energy_stats.append(StatisticData(
                    start=reading.period_start,
                    state=reading.value,
                    sum=cumulative_energy,
                ))
                cost_stats.append(StatisticData(
                    start=reading.period_start,
                    state=reading.value * price,
                    sum=cumulative_cost,
                ))

            # Anchor current (incomplete) month at zero so the graph doesn't
            # extrapolate forward from the last real data point.
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
