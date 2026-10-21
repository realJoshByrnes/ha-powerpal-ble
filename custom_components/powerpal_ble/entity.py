from __future__ import annotations

from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PowerpalCoordinator


class PowerpalEntity(CoordinatorEntity[PowerpalCoordinator]):
    """Shared device info and unique ids for all Powerpal entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PowerpalCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.serial)},
            name=f"Powerpal {coordinator.serial}",
            manufacturer="Powerpal",
            model="Powerpal",
            serial_number=coordinator.serial,
            connections={(CONNECTION_BLUETOOTH, coordinator.config_entry.data[CONF_ADDRESS])},
        )

    @property
    def available(self) -> bool:
        # Entities exist from setup but have no data until the first successful poll.
        return super().available and self.coordinator.data is not None
