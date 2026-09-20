"""Data models for PolEnergia API."""

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

from .tariffs import (
    DIRECTION_EXPORT,
    DIRECTION_IMPORT,
    split_direction,
    zone_label_to_key,
)

_LOGGER = logging.getLogger(__name__)

# Polenergia is Polish; bare timestamps without offset = Warsaw wall-clock.
POLENERGIA_TZ = ZoneInfo("Europe/Warsaw")

# Keys a reading row may use for its timestamp / value / owning meter.
_TIMESTAMP_KEYS = ("date", "timestamp", "readingDate")
_VALUE_KEYS = ("amount", "value", "consumption")
_MP_KEYS = ("measurementPointId", "measurementPointID", "mpId")

# Keys that may carry a zone label on a single row (long form).
_ZONE_KEYS = (
    "zone",
    "zoneName",
    "zoneLabel",
    "zoneId",
    "zoneNumber",
    "tariffZone",
    "strefa",
    "register",
    "registerName",
    "timeZone",
)

# Keys that may carry an explicit flow direction, independent of the zone label.
_DIRECTION_KEYS = ("direction", "flow", "kierunek", "obis", "obisCode", "registerCode")

# Container keys holding per-zone values on one row (wide form).
_ZONES_CONTAINER_KEYS = ("zones", "strefy", "zoneValues")
_ZONE_NAMES_CONTAINER_KEYS = ("zonesName", "zoneNames", "zoneLabels")

# Wide form, named per-zone amount fields: amountZone1, valueStrefa2, consumptionZ3...
_WIDE_ZONE_RE = re.compile(
    r"^(?:amount|value|consumption)(?:zone|strefa|z|l|s)?([123])$", re.IGNORECASE
)
# Wide form, named day/night fields.
_WIDE_NAMED = {
    "amountday": "z1",
    "amountnight": "z2",
    "valueday": "z1",
    "valuenight": "z2",
    "amountdzien": "z1",
    "amountnoc": "z2",
    "dzien": "z1",
    "noc": "z2",
}

# Row keys the parser understands. Anything else is reported through
# diagnostics so a multi-zone or prosumer user can be asked about it once.
_KNOWN_KEYS = frozenset(
    {
        *_TIMESTAMP_KEYS,
        *_VALUE_KEYS,
        *_MP_KEYS,
        *_ZONE_KEYS,
        *_DIRECTION_KEYS,
        *_ZONES_CONTAINER_KEYS,
        *_ZONE_NAMES_CONTAINER_KEYS,
        "unit",
        "id",
    }
)


@dataclass
class MeasurementPoint:
    """Represents a measurement point (meter)."""

    id: str
    customer_number: str
    ppe: str
    address: str
    tariff: str | None = None
    agreement_id: str | None = None
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

        raw_agreement_id = data.get("agreementId")
        agreement_id = str(raw_agreement_id) if raw_agreement_id is not None else None

        agreement_status = data.get("agreementStatus")
        if not agreement_status and agreement_id is not None:
            agreement_status = agreement_id

        return cls(
            id=measurement_point_id,
            customer_number=customer_number,
            ppe=ppe_number,
            address=address,
            # The MeasurementPoints payload carries no tariff; it is stamped in
            # afterwards from the Agreements endpoint, joined on agreement_id.
            tariff=data.get("tariffName") or data.get("tariff"),
            agreement_id=agreement_id,
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
    # Tariff zone slug (z1/z2/z3), or None when the row carries no zone split.
    zone: str | None = None
    # "import" (drawn from the grid) or "export" (fed back by a prosumer).
    direction: str = DIRECTION_IMPORT

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "EnergyReading":
        """Create a single EnergyReading from one API row.

        Raises ``ValueError`` if the row carries no usable timestamp — callers
        skip such rows rather than fabricating an anchor that would corrupt the
        monthly statistics stream. For rows that may expand into several
        readings (per-zone payloads) use :func:`parse_readings` instead.
        """
        readings, _unknown = _parse_row(data)
        if not readings:
            raise ValueError(f"Reading has no usable value: {data!r}")
        return readings[0]

    @property
    def period_anchor(self) -> datetime:
        """Polenergia anchors monthly readings at last day of the month (timezone-aware)."""
        return self.timestamp if self.timestamp.tzinfo else self.timestamp.replace(tzinfo=POLENERGIA_TZ)


def _parse_timestamp(data: dict[str, Any]) -> datetime:
    """Read the period timestamp off a row, or raise ``ValueError``."""
    timestamp_str = None
    for key in _TIMESTAMP_KEYS:
        candidate = data.get(key)
        if isinstance(candidate, str):
            timestamp_str = candidate
            break
    if timestamp_str is None:
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
        timestamp = timestamp.replace(tzinfo=POLENERGIA_TZ)
    return timestamp


def _extract_value(data: dict[str, Any]) -> float:
    """Read the kWh value off a row.

    Explicit priority — an earlier key holding a legitimate 0 must win over
    falling through to the next key (a bare ``or`` chain would skip it).
    """
    for key in _VALUE_KEYS:
        raw_value = data.get(key)
        if raw_value is not None:
            return float(raw_value)
    return 0.0


def _extract_mp_id(data: dict[str, Any]) -> str | None:
    for key in _MP_KEYS:
        mp_id = data.get(key)
        if mp_id:
            return str(mp_id)
    return None


def _explicit_direction(data: dict[str, Any]) -> str | None:
    """Direction from a dedicated field, if the row has one."""
    for key in _DIRECTION_KEYS:
        if (raw := data.get(key)) is not None:
            direction, _rest = split_direction(raw)
            # split_direction defaults to import; only trust a real marker.
            if direction == DIRECTION_EXPORT or _rest != str(raw).strip().lower():
                return direction
    return None


def _make_reading(
    timestamp: datetime,
    value: float,
    unit: str,
    mp_id: str | None,
    zone: str | None,
    direction: str,
) -> EnergyReading:
    """Build a reading, routing a negative amount to the export direction.

    A negative import value is a grid feed-in, not negative consumption; summing
    it into the energy stream would drive the cumulative total backwards.
    """
    if value < 0:
        direction = DIRECTION_EXPORT
        value = abs(value)
    return EnergyReading(
        timestamp=timestamp,
        value=value,
        unit=unit,
        measurement_point_id=mp_id,
        zone=zone,
        direction=direction,
    )


def _zone_from_row(data: dict[str, Any]) -> tuple[str | None, str]:
    """Zone slug and flow direction from a long-form row."""
    direction = _explicit_direction(data) or DIRECTION_IMPORT
    for key in _ZONE_KEYS:
        if (raw := data.get(key)) is None:
            continue
        label_direction, label = split_direction(raw)
        if _explicit_direction(data) is None:
            direction = label_direction
        return zone_label_to_key(label), direction
    return None, direction


def _zone_legend_map(legend: Any) -> dict[int, str | None]:
    """Map positional zone index -> slug from an envelope legend.

    Energa ships ``zones: [{"index": 0, "label": "Strefa 1 (dzienna):"}, ...]``
    alongside the chart rows.
    """
    mapping: dict[int, str | None] = {}
    if not isinstance(legend, list):
        return mapping
    for position, item in enumerate(legend):
        if isinstance(item, dict):
            index = item.get("index", position)
            label = item.get("label", item.get("name", ""))
            try:
                mapping[int(index)] = zone_label_to_key(label)
            except (TypeError, ValueError):
                continue
        elif isinstance(item, str):
            mapping[position] = zone_label_to_key(item)
    return mapping


def _parse_row(
    data: dict[str, Any], legend: dict[int, str | None] | None = None
) -> tuple[list[EnergyReading], set[str]]:
    """Parse one API row into one or more readings, plus unrecognised keys."""
    if not isinstance(data, dict):
        return [], set()

    timestamp = _parse_timestamp(data)
    unit = data.get("unit") or "kWh"
    mp_id = _extract_mp_id(data)
    unknown = {key for key in data if key not in _KNOWN_KEYS}
    readings: list[EnergyReading] = []

    row_direction = _explicit_direction(data) or DIRECTION_IMPORT

    # Shape 1: a per-zone container on the row (positional list or dict).
    container = next(
        (data[key] for key in _ZONES_CONTAINER_KEYS if isinstance(data.get(key), list | dict)),
        None,
    )
    if isinstance(container, list) and container and not all(
        isinstance(item, dict) for item in container
    ):
        legend = legend or {}
        for index, raw_value in enumerate(container):
            if raw_value is None:
                continue
            zone = legend.get(index, f"z{index + 1}" if index < 3 else f"z{index + 1}")
            readings.append(
                _make_reading(timestamp, float(raw_value), unit, mp_id, zone, row_direction)
            )
        if readings:
            return _collapse_single_zone(readings), unknown
    elif isinstance(container, dict):
        names: dict[Any, Any] = next(
            (data[key] for key in _ZONE_NAMES_CONTAINER_KEYS if isinstance(data.get(key), dict)),
            {},
        )
        for raw_key, raw_value in container.items():
            if raw_value is None or isinstance(raw_value, dict | list):
                continue
            zone = zone_label_to_key(names.get(raw_key, raw_key))
            readings.append(
                _make_reading(timestamp, float(raw_value), unit, mp_id, zone, row_direction)
            )
        if readings:
            return _collapse_single_zone(readings), unknown

    # Shape 2: named per-zone amount fields on one row.
    for raw_key, raw_value in data.items():
        if raw_value is None or not isinstance(raw_value, int | float):
            continue
        lowered = raw_key.lower()
        zone = None
        if (match := _WIDE_ZONE_RE.match(raw_key)) is not None:
            zone = f"z{match.group(1)}"
        elif lowered in _WIDE_NAMED:
            zone = _WIDE_NAMED[lowered]
        if zone is not None:
            unknown.discard(raw_key)
            readings.append(
                _make_reading(timestamp, float(raw_value), unit, mp_id, zone, row_direction)
            )
    if readings:
        return _collapse_single_zone(readings), unknown

    # Shape 3: a flat row, optionally annotated with a single zone label.
    zone, direction = _zone_from_row(data)
    return (
        [_make_reading(timestamp, _extract_value(data), unit, mp_id, zone, direction)],
        unknown,
    )


def _collapse_single_zone(readings: list[EnergyReading]) -> list[EnergyReading]:
    """Drop the zone dimension when a payload turned out to have exactly one zone.

    A G11 meter whose chart still ships a one-element ``zones`` array must not
    grow a ``_z1`` statistic stream next to its total.
    """
    zones = {reading.zone for reading in readings}
    if len(zones) == 1 and readings[0].zone in {None, "z1"}:
        for reading in readings:
            reading.zone = None
    return readings


def parse_readings(
    rows: Any, envelope: dict[str, Any] | None = None
) -> tuple[list[EnergyReading], set[str]]:
    """Parse an API readings payload into readings plus unrecognised field names.

    Accepts every shape observed across Polish operator APIs: flat rows, one row
    per zone (with the zone as a label, possibly carrying an ``A+``/``A-``
    direction marker), and wide rows holding a per-zone list or dict. Rows that
    cannot be parsed are skipped with a warning rather than failing the refresh.
    """
    legend = _zone_legend_map((envelope or {}).get("zones"))
    readings: list[EnergyReading] = []
    unknown_fields: set[str] = set()

    for row in rows or []:
        try:
            parsed, unknown = _parse_row(row, legend)
        except ValueError as err:
            _LOGGER.warning("Skipping unparseable reading: %s", err)
            continue
        except (TypeError, KeyError) as err:
            _LOGGER.warning("Skipping malformed reading %r: %s", row, err)
            continue
        readings.extend(parsed)
        unknown_fields |= unknown

    return readings, unknown_fields


def aggregate_readings(readings: list[EnergyReading]) -> list[EnergyReading]:
    """Sum readings sharing a (meter, period, zone, direction) key.

    External statistics are keyed by ``start``; two rows landing on the same
    anchor would otherwise produce duplicate points and a wrong cumulative sum.
    """
    grouped: OrderedDict[tuple[str | None, datetime, str | None, str], EnergyReading] = OrderedDict()
    for reading in readings:
        key = (
            reading.measurement_point_id,
            reading.period_anchor,
            reading.zone,
            reading.direction,
        )
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = reading
        else:
            existing.value += reading.value
    return list(grouped.values())


@dataclass
class PolEnergiaData:
    """Container for all PolEnergia account data."""

    customer_number: str
    measurement_points: list[MeasurementPoint]
    readings: dict[str, list[EnergyReading]]  # keyed by measurement point ID
    account_name: str | None = None
    last_update: datetime | None = None
    # Tariff code per agreement id, as returned by the Agreements endpoint.
    tariffs: dict[str, str] = field(default_factory=dict)

    def get_latest_reading(self, measurement_point_id: str) -> EnergyReading | None:
        """Get the latest import reading for a measurement point."""
        readings = [
            reading
            for reading in self.readings.get(measurement_point_id, [])
            if reading.direction == DIRECTION_IMPORT
        ]
        if not readings:
            return None
        latest = max(readings, key=lambda r: r.timestamp)
        # Multi-zone meters report one row per zone; the sensor shows the month.
        same_period = [r for r in readings if r.period_anchor == latest.period_anchor]
        if len(same_period) == 1:
            return latest
        total = sum(r.value for r in same_period)
        return EnergyReading(
            timestamp=latest.timestamp,
            value=total,
            unit=latest.unit,
            measurement_point_id=latest.measurement_point_id,
            zone=None,
            direction=DIRECTION_IMPORT,
        )
