"""Diagnostics support for My PolEnergia."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import PolEnergiaConfigEntry
from .polenergia.data import EnergyReading
from .polenergia.tariffs import zone_count

# PII that lands in files users attach to public GitHub issues.
TO_REDACT = {"address", "ppe"}

# Reading-row keys whose string values are safe to show verbatim in a sample.
# Everything else that is a string gets redacted, so a user can hand over the
# payload *shape* of a multi-zone or prosumer meter without leaking their PPE.
_SAMPLE_SAFE_KEYS = frozenset(
    {
        "date",
        "timestamp",
        "readingDate",
        "unit",
        "zone",
        "zoneName",
        "zoneLabel",
        "zoneId",
        "zoneNumber",
        "tariffZone",
        "strefa",
        "register",
        "registerName",
        "direction",
        "flow",
        "obis",
        "obisCode",
    }
)


def _redact_sample_row(row: dict[str, Any]) -> dict[str, Any]:
    """Keep numbers and zone/label strings; redact every other string value."""
    redacted: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, str) and key not in _SAMPLE_SAFE_KEYS:
            redacted[key] = "**REDACTED**"
        elif isinstance(value, dict):
            redacted[key] = _redact_sample_row(value)
        elif isinstance(value, list):
            redacted[key] = [
                _redact_sample_row(item) if isinstance(item, dict) else item for item in value
            ]
        else:
            redacted[key] = value
    return redacted


def _summarise_readings(readings: list[EnergyReading]) -> dict[str, Any]:
    """Shape of one meter's readings: how many, spanning what, and how stale.

    Values are deliberately excluded — consumption figures are the user's, and
    the counts and dates are what make a bug report actionable.
    """
    if not readings:
        return {"count": 0, "first_period": None, "last_period": None, "zones": []}

    anchors = sorted(reading.period_anchor for reading in readings)
    return {
        "count": len(readings),
        "first_period": anchors[0].isoformat(),
        "last_period": anchors[-1].isoformat(),
        "zones": sorted({r.zone for r in readings if r.zone is not None}),
        "directions": sorted({r.direction for r in readings}),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: PolEnergiaConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    diagnostics_data: dict[str, Any] = {
        "entry_data": {
            "customer_number": entry.data.get("customer_number"),
            "scan_interval": entry.options.get("scan_interval"),
            "import_price": entry.options.get("import_price"),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
        },
    }

    client = getattr(coordinator, "client", None)
    diagnostics_data["tariffs"] = {
        "zones_seen": coordinator.zones_seen,
        # Unrecognised keys and a redacted sample row are the evidence needed to
        # confirm how (or whether) Polenergia splits readings by tariff zone.
        "unknown_reading_fields": sorted(
            getattr(client, "last_unknown_reading_fields", set()) or set()
        ),
        "readings_envelope_keys": list(getattr(client, "last_envelope_keys", []) or []),
        "reading_sample": [
            _redact_sample_row(row)
            for row in (getattr(client, "last_reading_sample", []) or [])
        ],
    }

    if coordinator.data and coordinator.data.get("data"):
        data = coordinator.data["data"]
        diagnostics_data["tariffs"]["tariff_by_agreement"] = data.tariffs
        diagnostics_data["data"] = {
            "customer_number": data.customer_number,
            "measurement_points_count": len(data.measurement_points),
            "measurement_points": [
                {
                    "id": mp.id,
                    "ppe": mp.ppe,
                    "tariff": mp.tariff,
                    "zone_count": zone_count(mp.tariff),
                    "address": mp.address,
                    "agreement_id": mp.agreement_id,
                    "agreement_status": mp.agreement_status,
                }
                for mp in data.measurement_points
            ],
            "readings_count": {
                mp_id: len(readings)
                for mp_id, readings in data.readings.items()
            },
            # Per-meter reading summary: enough to tell "no data at all" from
            # "data, but stale" without the user pasting any actual readings.
            "readings_summary": {
                mp_id: _summarise_readings(readings)
                for mp_id, readings in data.readings.items()
            },
            "last_update": data.last_update.isoformat()
            if data.last_update
            else None,
        }

    return async_redact_data(diagnostics_data, TO_REDACT)
