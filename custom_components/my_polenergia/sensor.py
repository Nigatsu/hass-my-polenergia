"""Sensor platform for My PolEnergia integration."""

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ACCOUNT_NAME,
    ATTR_ADDRESS,
    ATTR_CUSTOMER_NUMBER,
    ATTR_LAST_UPDATE,
    ATTR_PPE,
    ATTR_TARIFF,
    CONF_IMPORT_PRICE,
    CURRENCY_PLN,
    DEFAULT_IMPORT_PRICE,
    DOMAIN,
)
from .hass_integration.coordinator import PolEnergiaDataUpdateCoordinator
from .polenergia.data import MeasurementPoint

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PolEnergia sensors."""
    coordinator: PolEnergiaDataUpdateCoordinator = entry.runtime_data

    entities = []
    if coordinator.data and coordinator.data.get("data"):
        data = coordinator.data["data"]
        for mp in data.measurement_points:
            entities.append(PolEnergiaMonthlyConsumptionSensor(coordinator, mp, entry))
            entities.append(PolEnergiaImportPriceSensor(coordinator, mp, entry))

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d sensor entities", len(entities))
    else:
        _LOGGER.warning("No sensors created — no measurement points found")


class PolEnergiaBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for PolEnergia sensors."""

    def __init__(
        self,
        coordinator: PolEnergiaDataUpdateCoordinator,
        measurement_point: MeasurementPoint,
        entry: ConfigEntry,
    ):
        super().__init__(coordinator)
        self.measurement_point = measurement_point
        self._entry = entry

        self._attr_has_entity_name = True
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, measurement_point.ppe)},
            name=measurement_point.display_name,
            manufacturer="Polenergia",
            model="Smart Meter",
        )

    @property
    def extra_state_attributes(self):
        attrs = {
            ATTR_PPE: self.measurement_point.ppe,
            ATTR_CUSTOMER_NUMBER: self.measurement_point.customer_number,
            ATTR_ADDRESS: self.measurement_point.address,
        }

        if self.measurement_point.tariff:
            attrs[ATTR_TARIFF] = self.measurement_point.tariff

        if self.coordinator.data and self.coordinator.data.get("data"):
            data = self.coordinator.data["data"]
            if data.account_name:
                attrs[ATTR_ACCOUNT_NAME] = data.account_name
            if data.last_update:
                attrs[ATTR_LAST_UPDATE] = data.last_update.isoformat()

        return attrs


class PolEnergiaMonthlyConsumptionSensor(PolEnergiaBaseSensor):
    """Latest monthly consumption reading (informational — not cumulative)."""

    def __init__(
        self,
        coordinator: PolEnergiaDataUpdateCoordinator,
        measurement_point: MeasurementPoint,
        entry: ConfigEntry,
    ):
        super().__init__(coordinator, measurement_point, entry)

        self._attr_translation_key = "last_month_consumption"
        self._attr_name = "Last Month Consumption"
        self._attr_unique_id = f"{measurement_point.id}_reading"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:counter"

    @property
    def native_value(self):
        if not self.coordinator.data or not self.coordinator.data.get("data"):
            return None
        data = self.coordinator.data["data"]
        latest = data.get_latest_reading(self.measurement_point.id)
        return latest.value if latest else None

    @property
    def extra_state_attributes(self):
        attrs = super().extra_state_attributes
        if self.coordinator.data and self.coordinator.data.get("data"):
            data = self.coordinator.data["data"]
            latest = data.get_latest_reading(self.measurement_point.id)
            if latest:
                attrs["period"] = latest.timestamp.strftime("%Y-%m")
        return attrs


class PolEnergiaImportPriceSensor(PolEnergiaBaseSensor):
    """Diagnostic sensor showing the configured import price (PLN/kWh)."""

    def __init__(
        self,
        coordinator: PolEnergiaDataUpdateCoordinator,
        measurement_point: MeasurementPoint,
        entry: ConfigEntry,
    ):
        super().__init__(coordinator, measurement_point, entry)

        self._attr_translation_key = "import_price"
        self._attr_name = "Import Price"
        self._attr_unique_id = f"{measurement_point.id}_import_price"
        self._attr_native_unit_of_measurement = f"{CURRENCY_PLN}/kWh"
        self._attr_icon = "mdi:currency-eur"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_suggested_display_precision = 4

    @property
    def native_value(self):
        return float(self._entry.options.get(CONF_IMPORT_PRICE, DEFAULT_IMPORT_PRICE))
