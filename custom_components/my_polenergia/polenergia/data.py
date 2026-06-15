"""Data models for PolEnergia API."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

# Polenergia is Polish; bare timestamps without offset = Warsaw wall-clock.
_POLENERGIA_TZ = ZoneInfo("Europe/Warsaw")


@dataclass
class MeasurementPoint:
    """Represents a measurement point (meter)."""

    id: str
    customer_number: str
    ppe: str
    address: str
    tariff: str | None = None
    agreement_status: str | None = None
    raw_data: dict[str, Any] | None = None

    @property
    def display_name(self) -> str:
        """Human-readable name for HA frontend (address or PPE fallback)."""
        return self.address or self.ppe

    @classmethod
    def from_api_response(cls, data: dict[str, Any], customer_number: str) -> "MeasurementPoint":
        """Create MeasurementPoint from API response."""
        measurement_point_id = str(data.get("measurementPointId") or data.get("id", ""))
        ppe_number = str(data.get("number") or data.get("ppe", ""))

        address_line1 = data.get("addressLine1", "")
        address_line2 = data.get("addressLine2", "")
        address = f"{address_line1}, {address_line2}".strip(", ") if address_line1 else address_line2 or ""

        agreement_status = data.get("agreementStatus")
        if not agreement_status and data.get("agreementId") is not None:
            agreement_status = str(data.get("agreementId"))

        return cls(
            id=measurement_point_id,
            customer_number=customer_number,
            ppe=ppe_number,
            address=address,
            tariff=data.get("tariffName") or data.get("tariff"),
            agreement_status=agreement_status,
            raw_data=data,
        )


@dataclass
class EnergyReading:
    """Represents a monthly energy reading."""

    timestamp: datetime       # End of billing period
    value: float              # kWh consumed in this period
    unit: str
    measurement_point_id: str | None = None

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "EnergyReading":
        """Create EnergyReading from API response.

        Raises ``ValueError`` if the row carries no usable timestamp — callers
        skip such rows rather than fabricating an anchor that would corrupt the
        monthly statistics stream.
        """
        timestamp_str = data.get("date") or data.get("timestamp") or data.get("readingDate")
        if not isinstance(timestamp_str, str):
            raise ValueError(f"Reading has no timestamp: {data!r}")

        for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"]:
            try:
                timestamp = datetime.strptime(timestamp_str, fmt)
                break
            except ValueError:
                continue
        else:
            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))

        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=_POLENERGIA_TZ)

        # Explicit priority — an earlier key holding a legitimate 0 must win over
        # falling through to the next key (a bare ``or`` chain would skip it).
        raw_value = data.get("amount")
        if raw_value is None:
            raw_value = data.get("value")
        if raw_value is None:
            raw_value = data.get("consumption")
        if raw_value is None:
            raw_value = 0

        mp_id = data.get("measurementPointId")
        return cls(
            timestamp=timestamp,
            value=float(raw_value),
            unit=data.get("unit", "kWh"),
            measurement_point_id=str(mp_id) if mp_id else None,
        )

    @property
    def period_anchor(self) -> datetime:
        """Polenergia anchors monthly readings at last day of the month (timezone-aware)."""
        return self.timestamp if self.timestamp.tzinfo else self.timestamp.replace(tzinfo=_POLENERGIA_TZ)


@dataclass
class PolEnergiaData:
    """Container for all PolEnergia account data."""

    customer_number: str
    measurement_points: list[MeasurementPoint]
    readings: dict[str, list[EnergyReading]]  # keyed by measurement point ID
    account_name: str | None = None
    last_update: datetime | None = None

    def get_latest_reading(self, measurement_point_id: str) -> EnergyReading | None:
        """Get the latest reading for a measurement point."""
        readings = self.readings.get(measurement_point_id, [])
        if not readings:
            return None
        return max(readings, key=lambda r: r.timestamp)
