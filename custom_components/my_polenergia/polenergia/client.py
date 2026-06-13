"""PolEnergia API client - high-level interface."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from .connector import PolEnergiaConnector
from .data import EnergyReading, MeasurementPoint, PolEnergiaData
from .errors import PolEnergiaAPIError, PolEnergiaNoDataError

_LOGGER = logging.getLogger(__name__)


def _next_day_utc() -> datetime:
    """Tomorrow at 00:00 UTC — exclusive upper bound for half-open [from, to) queries."""
    now = datetime.now(tz=timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


class PolEnergiaClient:
    """High-level client for PolEnergia API."""

    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._connector = PolEnergiaConnector(session=session)

    @property
    def connector(self) -> PolEnergiaConnector:
        return self._connector

    async def __aenter__(self):
        await self._connector.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._connector.__aexit__(exc_type, exc_val, exc_tb)

    async def authenticate(self, username: str, password: str) -> bool:
        return await self._connector.authenticate(username, password)

    async def close(self):
        await self._connector.close()

    async def get_customer_numbers(self) -> list[str]:
        response = await self._connector.get("accounts/customerNumbers")
        if isinstance(response, list):
            return [str(n) for n in response]
        if isinstance(response, dict):
            return [str(n) for n in response.get("customerNumbers", response.get("data", []))]
        raise PolEnergiaAPIError(f"Unexpected response format: {type(response)}")

    async def get_account_name(self, customer_number: str) -> str | None:
        """Get the account holder's name."""
        try:
            response = await self._connector.get("accounts", params={"customerNumber": customer_number})
            if isinstance(response, dict):
                name = response.get("name") or response.get("correspondenceAddressName")
                return name.strip() if name else None
        except Exception as err:
            _LOGGER.warning("Could not fetch account name: %s", err)
        return None

    async def get_agreements(self, customer_number: str) -> list[dict[str, Any]]:
        response = await self._connector.get("Agreements", params={"customerNumber": customer_number})
        if isinstance(response, list):
            return response
        if isinstance(response, dict):
            return response.get("results", response.get("data", []))
        return []

    async def get_earliest_agreement_date(self, customer_number: str) -> datetime | None:
        try:
            agreements = await self.get_agreements(customer_number)
            dates = []
            for agreement in agreements:
                date_str = agreement.get("dateFrom")
                if date_str:
                    try:
                        dates.append(datetime.fromisoformat(date_str.replace("Z", "+00:00")))
                    except (ValueError, TypeError):
                        pass
            return min(dates) if dates else None
        except Exception as err:
            _LOGGER.error("Failed to get earliest agreement date: %s", err)
            return None

    async def get_measurement_points(self, customer_number: str) -> list[MeasurementPoint]:
        params = {"customerNumber": customer_number, "agreementStatusFilter": "Active"}
        response = await self._connector.get("MeasurementPoints", params=params)

        if isinstance(response, list):
            data = response
        elif isinstance(response, dict):
            data = response.get("results", response.get("data", []))
        else:
            raise PolEnergiaAPIError(f"Unexpected response format: {type(response)}")

        if not data:
            raise PolEnergiaNoDataError("No measurement points found")

        return [MeasurementPoint.from_api_response(mp, customer_number) for mp in data]

    async def get_readings(
        self,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
    ) -> list[EnergyReading]:
        """Get monthly energy readings."""
        if to_date is None:
            to_date = _next_day_utc()
        if from_date is None:
            from_date = to_date - timedelta(days=365)

        params = {
            "from": from_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "to": to_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }

        response = await self._connector.get("MeasurementPoints/readings", params=params)

        if isinstance(response, list):
            readings_data = response
        elif isinstance(response, dict):
            readings_data = response.get("readings", response.get("data", []))
        else:
            raise PolEnergiaAPIError(f"Unexpected response format: {type(response)}")

        readings: list[EnergyReading] = []
        for r in readings_data:
            try:
                readings.append(EnergyReading.from_api_response(r))
            except ValueError as err:
                _LOGGER.warning("Skipping unparseable reading: %s", err)
        return readings

    async def get_all_data(self, customer_number: str) -> PolEnergiaData:
        """Get all current data for the account."""
        measurement_points = await self.get_measurement_points(customer_number)

        # Fetch last 13 months to cover current + previous year
        to_date = _next_day_utc()
        from_date = datetime(to_date.year - 1, to_date.month, 1, tzinfo=timezone.utc)

        readings_list = await self.get_readings(from_date=from_date, to_date=to_date)

        # Group readings by measurementPointId from the API response
        all_readings: dict[str, list[EnergyReading]] = {mp.id: [] for mp in measurement_points}
        for reading in readings_list:
            if reading.measurement_point_id and reading.measurement_point_id in all_readings:
                all_readings[reading.measurement_point_id].append(reading)
            else:
                # Fallback: assign to all measurement points (single-meter accounts)
                for mp_id in all_readings:
                    all_readings[mp_id].append(reading)

        account_name = await self.get_account_name(customer_number)

        return PolEnergiaData(
            customer_number=customer_number,
            measurement_points=measurement_points,
            readings=all_readings,
            account_name=account_name,
            last_update=datetime.now(tz=timezone.utc),
        )

    @property
    def is_authenticated(self) -> bool:
        return self._connector.is_authenticated
