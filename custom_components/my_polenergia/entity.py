"""Shared base entity for the My PolEnergia integration."""

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ACCOUNT_NAME,
    ATTR_ADDRESS,
    ATTR_CUSTOMER_NUMBER,
    ATTR_LAST_UPDATE,
    ATTR_PPE,
    ATTR_TARIFF,
    ATTR_ZONE_COUNT,
    DOMAIN,
)
from .coordinator import PolEnergiaConfigEntry, PolEnergiaDataUpdateCoordinator
from .polenergia.data import MeasurementPoint
from .polenergia.tariffs import zone_count


class PolEnergiaEntity(CoordinatorEntity[PolEnergiaDataUpdateCoordinator]):
    """Base entity bound to one measurement point."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PolEnergiaDataUpdateCoordinator,
        measurement_point: MeasurementPoint,
        entry: PolEnergiaConfigEntry,
    ) -> None:
        """Initialise the entity and its device."""
        super().__init__(coordinator)
        self.measurement_point = measurement_point
        self._entry = entry

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, measurement_point.ppe)},
            name=measurement_point.display_name,
            manufacturer="Polenergia",
            model="Smart Meter",
        )

    @property
    def available(self) -> bool:
        """Available while the coordinator succeeds *and* this meter still exists.

        A measurement point can disappear from the account between refreshes;
        without this check the entity would keep serving its last known value.
        """
        if not super().available:
            return False
        data = (self.coordinator.data or {}).get("data")
        if data is None:
            return False
        return any(mp.id == self.measurement_point.id for mp in data.measurement_points)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Account and meter metadata shared by every entity."""
        attrs: dict[str, Any] = {
            ATTR_PPE: self.measurement_point.ppe,
            ATTR_CUSTOMER_NUMBER: self.measurement_point.customer_number,
            ATTR_ADDRESS: self.measurement_point.address,
        }

        if self.measurement_point.tariff:
            attrs[ATTR_TARIFF] = self.measurement_point.tariff
            attrs[ATTR_ZONE_COUNT] = zone_count(self.measurement_point.tariff)

        data = (self.coordinator.data or {}).get("data")
        if data is not None:
            if data.account_name:
                attrs[ATTR_ACCOUNT_NAME] = data.account_name
            if data.last_update:
                attrs[ATTR_LAST_UPDATE] = data.last_update.isoformat()

        return attrs
