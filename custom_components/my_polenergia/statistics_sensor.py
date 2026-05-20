"""Statistics-only sensors that feed the HA recorder (Energy Dashboard)."""

import logging

from homeassistant.components.recorder.models import StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_IMPORT_PRICE, CURRENCY_PLN, DEFAULT_IMPORT_PRICE, DOMAIN
from .hass_integration.coordinator import PolEnergiaDataUpdateCoordinator
from .polenergia.data import MeasurementPoint

_LOGGER = logging.getLogger(__name__)


class _PolEnergiaStatisticsBase(CoordinatorEntity, SensorEntity):
    """Common machinery for statistics-only sensors.

    Subclasses set:
        STAT_KEY              — key in coordinator.data["statistics"][mp_id]
        _attr_name            — entity name (e.g. "Historical Statistics")
        _attr_translation_key — translation key
        _unit                 — unit of measurement
        _device_class         — SensorDeviceClass
        _unit_class           — recorder statistics unit_class ("energy"/"monetary")
        _unique_suffix        — appended to measurement_point.id for unique_id
    """

    STAT_KEY: str = ""
    _unit: str = ""
    _device_class: SensorDeviceClass | None = None
    _unit_class: str | None = None
    _unique_suffix: str = ""
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: PolEnergiaDataUpdateCoordinator,
        measurement_point: MeasurementPoint,
        entry: ConfigEntry,
    ):
        super().__init__(coordinator)
        self.hass = hass
        self.measurement_point = measurement_point
        self._entry = entry

        self._attr_has_entity_name = True
        self._attr_unique_id = f"{measurement_point.id}_{self._unique_suffix}"
        self._attr_native_unit_of_measurement = self._unit
        self._attr_device_class = self._device_class

        _LOGGER.info(
            "Initialized %s sensor for measurement point %s (PPE: %s)",
            self.STAT_KEY,
            measurement_point.id,
            measurement_point.ppe,
        )

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.measurement_point.ppe)},
            name=self.measurement_point.display_name,
            manufacturer="Polenergia",
            model="Smart Meter",
        )

    def _get_stats(self) -> list:
        if not self.coordinator.data:
            return []
        statistics_data = self.coordinator.data.get("statistics", {})
        mp_entry = statistics_data.get(self.measurement_point.id, {})
        if isinstance(mp_entry, list):
            # Legacy shape — only energy stream. Map by key.
            return mp_entry if self.STAT_KEY == "energy" else []
        return mp_entry.get(self.STAT_KEY, [])

    def _latest_state_from_readings(self) -> float | None:
        """Fallback state when in-memory stats dict is empty (e.g. after HA restart)."""
        if not self.coordinator.data:
            return None
        data = self.coordinator.data.get("data")
        if not data:
            return None
        latest = data.get_latest_reading(self.measurement_point.id)
        if latest is None:
            return None
        if self.STAT_KEY == "energy":
            return float(latest.value)
        # cost: latest kWh × configured PLN/kWh
        price = float(self._entry.options.get(CONF_IMPORT_PRICE, DEFAULT_IMPORT_PRICE))
        return float(latest.value) * price

    @property
    def native_value(self) -> float | None:
        """Live state: latest cumulative sum if just imported, else latest reading."""
        mp_statistics = self._get_stats()
        if mp_statistics:
            return mp_statistics[-1]["sum"]
        return self._latest_state_from_readings()

    @property
    def available(self) -> bool:
        return self.native_value is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        mp_statistics = self._get_stats()

        if mp_statistics:
            _LOGGER.info(
                "Importing %d %s statistics for %s",
                len(mp_statistics),
                self.STAT_KEY,
                self.entity_id,
            )

            metadata = StatisticMetaData(
                source="recorder",
                statistic_id=self.entity_id,
                name=self._attr_name,
                unit_of_measurement=self._unit,
                unit_class=self._unit_class,
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
            )

            async_import_statistics(self.hass, metadata, mp_statistics)

        super()._handle_coordinator_update()

    @property
    def extra_state_attributes(self):
        mp_statistics = self._get_stats()

        attrs = {
            "ppe": self.measurement_point.ppe,
            "customer_number": self.measurement_point.customer_number,
            "address": self.measurement_point.address,
            "statistics_count": len(mp_statistics),
        }

        if mp_statistics:
            attrs["first_statistic"] = mp_statistics[0]["start"].isoformat()
            attrs["last_statistic"] = mp_statistics[-1]["start"].isoformat()
            attrs["total_sum"] = mp_statistics[-1]["sum"]

        return attrs


class PolEnergiaStatisticsSensor(_PolEnergiaStatisticsBase):
    """Cumulative energy (kWh) statistics-only sensor for Energy Dashboard."""

    STAT_KEY = "energy"
    _unit = UnitOfEnergy.KILO_WATT_HOUR
    _device_class = SensorDeviceClass.ENERGY
    _unit_class = "energy"
    _unique_suffix = "statistics"

    def __init__(self, hass, coordinator, measurement_point, entry):
        self._attr_translation_key = "historical_statistics"
        self._attr_name = "Historical Statistics"
        self._attr_icon = "mdi:chart-line"
        super().__init__(hass, coordinator, measurement_point, entry)


class PolEnergiaCostStatisticsSensor(_PolEnergiaStatisticsBase):
    """Cumulative cost (PLN) statistics-only sensor for Energy Dashboard cost tracking."""

    STAT_KEY = "cost"
    _unit = CURRENCY_PLN
    _device_class = SensorDeviceClass.MONETARY
    _unit_class = None  # PLN has no HA unit converter
    _unique_suffix = "cost_statistics"

    def __init__(self, hass, coordinator, measurement_point, entry):
        self._attr_translation_key = "cost_statistics"
        self._attr_name = "Cost Statistics"
        self._attr_icon = "mdi:cash-multiple"
        super().__init__(hass, coordinator, measurement_point, entry)
