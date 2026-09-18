"""PolEnergia API client - high-level interface."""

from datetime import UTC, datetime, timedelta
import logging
from typing import Any

import aiohttp

from .connector import PolEnergiaConnector
from .data import (
    EnergyReading,
    MeasurementPoint,
    PolEnergiaData,
    aggregate_readings,
    parse_readings,
)
from .errors import PolEnergiaAPIError, PolEnergiaNoDataError
from .tariffs import DIRECTION_EXPORT

_LOGGER = logging.getLogger(__name__)


def _next_day_utc() -> datetime:
    """Tomorrow at 00:00 UTC — exclusive upper bound for half-open [from, to) queries."""
    now = datetime.now(tz=UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


class PolEnergiaClient:
    """High-level client for PolEnergia API."""

    def __init__(self, session: aiohttp.ClientSession | None = None):
        self._connector = PolEnergiaConnector(session=session)
        # Memo so one refresh hits /Agreements once for both the tariff map and
        # the earliest agreement date. Reset at the start of get_all_data.
        self._agreements_cache: dict[str, list[dict[str, Any]]] = {}
        # Evidence captured from the last readings call, surfaced in diagnostics
        # so a multi-zone or prosumer user can be asked about the payload once.
        self.last_unknown_reading_fields: set[str] = set()
        self.last_envelope_keys: list[str] = []
        self.last_reading_sample: list[dict[str, Any]] = []
        self._logged_export = False

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
        if (cached := self._agreements_cache.get(customer_number)) is not None:
            return cached

        response = await self._connector.get("Agreements", params={"customerNumber": customer_number})
        if isinstance(response, list):
            agreements = response
        elif isinstance(response, dict):
            agreements = response.get("results", response.get("data", []))
        else:
            agreements = []

        self._agreements_cache[customer_number] = agreements
        return agreements

    async def get_tariff_by_agreement(self, customer_number: str) -> dict[str, str]:
        """Map agreement id -> tariff code (e.g. ``{"62714": "G11"}``).

        The MeasurementPoints payload carries no tariff at all; the only place it
        appears is Agreements, under ``parameters[type == "Tariff"]``.
        """
        tariffs: dict[str, str] = {}
        try:
            agreements = await self.get_agreements(customer_number)
        except Exception as err:  # noqa: BLE001 - tariff is optional metadata
            _LOGGER.warning("Could not fetch agreements for tariff detection: %s", err)
            return tariffs

        for agreement in agreements:
            if not isinstance(agreement, dict):
                continue
            agreement_id = agreement.get("agreementId")
            parameters = agreement.get("parameters")
            if agreement_id is None or not isinstance(parameters, list):
                continue
            for parameter in parameters:
                if not isinstance(parameter, dict):
                    continue
                if str(parameter.get("type", "")).lower() == "tariff" and parameter.get("value"):
                    tariffs[str(agreement_id)] = str(parameter["value"])
                    break
        return tariffs

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

        envelope: dict[str, Any] | None = None
        if isinstance(response, list):
            readings_data = response
        elif isinstance(response, dict):
            envelope = response
            readings_data = response.get(
                "readings", response.get("results", response.get("data", []))
            )
        else:
            raise PolEnergiaAPIError(f"Unexpected response format: {type(response)}")

        readings, unknown_fields = parse_readings(readings_data, envelope)

        self.last_unknown_reading_fields = unknown_fields
        self.last_envelope_keys = sorted(envelope) if envelope else []
        self.last_reading_sample = [row for row in readings_data[:3] if isinstance(row, dict)]
        if unknown_fields:
            _LOGGER.debug(
                "Unrecognised fields in readings payload: %s", sorted(unknown_fields)
            )

        if not self._logged_export and any(r.direction == DIRECTION_EXPORT for r in readings):
            self._logged_export = True
            _LOGGER.info(
                "Export (feed-in) readings detected — they are kept out of the "
                "consumption statistics and written to a separate return stream"
            )

        # External statistics are keyed by start time; collapse rows that share a
        # (meter, period, zone, direction) key so no duplicate points are written.
        return aggregate_readings(readings)

    async def get_all_data(self, customer_number: str) -> PolEnergiaData:
        """Get all current data for the account."""
        # One /Agreements round-trip per refresh, shared with the tariff lookup
        # and get_earliest_agreement_date.
        self._agreements_cache.pop(customer_number, None)

        measurement_points = await self.get_measurement_points(customer_number)

        tariffs = await self.get_tariff_by_agreement(customer_number)
        if tariffs:
            sole_tariff = next(iter(tariffs.values())) if len(tariffs) == 1 else None
            for mp in measurement_points:
                mp.tariff = tariffs.get(mp.agreement_id or "") or sole_tariff or mp.tariff

        # Fetch last 13 months to cover current + previous year
        to_date = _next_day_utc()
        from_date = datetime(to_date.year - 1, to_date.month, 1, tzinfo=UTC)

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
            last_update=datetime.now(tz=UTC),
            tariffs=tariffs,
        )

    @property
    def is_authenticated(self) -> bool:
        return self._connector.is_authenticated
