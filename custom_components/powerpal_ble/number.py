from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PowerpalConfigEntry
from .ble.const import LED_SENSITIVITY_MAX, LED_SENSITIVITY_MIN
from .coordinator import PowerpalCoordinator
from .entity import PowerpalEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerpalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([PowerpalLedSensitivity(entry.runtime_data)])


class PowerpalLedSensitivity(PowerpalEntity, NumberEntity):
    """The sensitivity of the LED sensor on the meter. Stored on the device itself."""

    _attr_translation_key = "led_sensitivity"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = LED_SENSITIVITY_MIN
    _attr_native_max_value = LED_SENSITIVITY_MAX
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: PowerpalCoordinator) -> None:
        super().__init__(coordinator, "led_sensitivity")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.state.led_sensitivity

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_led_sensitivity(int(value))
