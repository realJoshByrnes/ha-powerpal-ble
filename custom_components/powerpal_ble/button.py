from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PowerpalConfigEntry
from .coordinator import PowerpalCoordinator
from .entity import PowerpalEntity

UPDATE_NOW = ButtonEntityDescription(key="update_now", translation_key="update_now")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerpalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([PowerpalUpdateButton(entry.runtime_data)])


class PowerpalUpdateButton(PowerpalEntity, ButtonEntity):
    """Poll the device right now instead of waiting for the next scheduled update."""

    def __init__(self, coordinator: PowerpalCoordinator) -> None:
        super().__init__(coordinator, UPDATE_NOW.key)
        self.entity_description = UPDATE_NOW

    async def async_press(self) -> None:
        await self.coordinator.async_refresh()
        if self.coordinator.last_poll_error:
            raise HomeAssistantError(f"Powerpal update failed: {self.coordinator.last_poll_error}")
