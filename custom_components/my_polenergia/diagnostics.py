"""Diagnostics support for My PolEnergia."""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    diagnostics_data = {
        "entry_data": {
            "customer_number": entry.data.get("customer_number"),
            "scan_interval": entry.options.get("scan_interval"),
            "import_price": entry.options.get("import_price"),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
        },
    }

    statistics = coordinator.data.get("statistics", {}) if coordinator.data else {}
    diagnostics_data["statistics_counts"] = {
        mp_id: {
            "energy": len(streams.get("energy", [])) if isinstance(streams, dict) else len(streams),
            "cost": len(streams.get("cost", [])) if isinstance(streams, dict) else 0,
        }
        for mp_id, streams in statistics.items()
    }

    if coordinator.data and coordinator.data.get("data"):
        data = coordinator.data["data"]
        diagnostics_data["data"] = {
            "customer_number": data.customer_number,
            "measurement_points_count": len(data.measurement_points),
            "measurement_points": [
                {
                    "id": mp.id,
                    "ppe": mp.ppe,
                    "tariff": mp.tariff,
                    "address": mp.address,
                    "agreement_status": mp.agreement_status,
                }
                for mp in data.measurement_points
            ],
            "readings_count": {
                mp_id: len(readings)
                for mp_id, readings in data.readings.items()
            },
            "last_update": data.last_update.isoformat()
            if data.last_update
            else None,
        }

    return diagnostics_data
