"""Sensor platform for My PolEnergia integration."""

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_IMPORT_PRICE,
    CURRENCY_PLN,
    DEFAULT_IMPORT_PRICE,
)
from .coordinator import PolEnergiaConfigEntry, PolEnergiaDataUpdateCoordinator
from .entity import PolEnergiaEntity
from .polenergia.data import MeasurementPoint

# All data comes from a single coordinator refresh; nothing here talks to the API.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PolEnergiaConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up PolEnergia sensors, including any meter added to the account later."""
    coordinator = entry.runtime_data
    known_ids: set[str] = set()

    @callback
    def _add_new_measurement_points() -> None:
        data = (coordinator.data or {}).get("data")
        if data is None:
            return
        new = [mp for mp in data.measurement_points if mp.id not in known_ids]
        if not new:
            return
        known_ids.update(mp.id for mp in new)
        async_add_entities(
            sensor for mp in new for sensor in _sensors_for(coordinator, mp, entry)
        )

    _add_new_measurement_points()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_measurement_points))


def _sensors_for(
    coordinator: PolEnergiaDataUpdateCoordinator,
    measurement_point: MeasurementPoint,
    entry: PolEnergiaConfigEntry,
) -> list[SensorEntity]:
    """The entity set created for one measurement point."""
    return [
        PolEnergiaMonthlyConsumptionSensor(coordinator, measurement_point, entry),
        PolEnergiaImportPriceSensor(coordinator, measurement_point, entry),
    ]


class PolEnergiaMonthlyConsumptionSensor(PolEnergiaEntity, SensorEntity):
    """Latest monthly consumption reading (informational — not cumulative)."""

    _attr_translation_key = "last_month_consumption"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY

    def __init__(
        self,
        coordinator: PolEnergiaDataUpdateCoordinator,
        measurement_point: MeasurementPoint,
        entry: PolEnergiaConfigEntry,
    ) -> None:
        """Initialise the consumption sensor."""
        super().__init__(coordinator, measurement_point, entry)
        self._attr_unique_id = f"{measurement_point.id}_reading"

    @property
    def native_value(self) -> float | None:
        """Consumption of the most recently published month, in kWh."""
        data = (self.coordinator.data or {}).get("data")
        if data is None:
            return None
        latest = data.get_latest_reading(self.measurement_point.id)
        return latest.value if latest else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Shared metadata plus the billing period the value belongs to."""
        attrs = super().extra_state_attributes
        data = (self.coordinator.data or {}).get("data")
        if data is not None:
            latest = data.get_latest_reading(self.measurement_point.id)
            if latest:
                attrs["period"] = latest.timestamp.strftime("%Y-%m")
        return attrs


class PolEnergiaImportPriceSensor(PolEnergiaEntity, SensorEntity):
    """Diagnostic sensor showing the configured import price (PLN/kWh)."""

    _attr_translation_key = "import_price"
    _attr_native_unit_of_measurement = f"{CURRENCY_PLN}/kWh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 4

    def __init__(
        self,
        coordinator: PolEnergiaDataUpdateCoordinator,
        measurement_point: MeasurementPoint,
        entry: PolEnergiaConfigEntry,
    ) -> None:
        """Initialise the price sensor."""
        super().__init__(coordinator, measurement_point, entry)
        self._attr_unique_id = f"{measurement_point.id}_import_price"

    @property
    def native_value(self) -> float:
        """The price used to compute the cost statistics stream."""
        return float(self._entry.options.get(CONF_IMPORT_PRICE, DEFAULT_IMPORT_PRICE))
