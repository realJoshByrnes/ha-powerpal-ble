"""Read-only, masked entries for the two secrets: the pairing code and the hardware UUID.

They are disabled by default. Home Assistant records entity states, so enabling one stores the
value in the recorder database; leave them off unless you need to look the value up.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PowerpalConfigEntry
from .const import CONF_CLOUD_TOKEN, CONF_HARDWARE_UUID, CONF_PAIRING_CODE
from .coordinator import PowerpalCoordinator
from .entity import PowerpalEntity


def _pairing_code(entry: ConfigEntry) -> str | None:
    code = entry.data.get(CONF_PAIRING_CODE)
    return None if code is None else f"{int(code):06d}"


def _hardware_uuid(entry: ConfigEntry) -> str | None:
    # The credential the web service accepted is the authoritative spelling; before that, show
    # the bytes read from the device as a standard UUID.
    if token := entry.data.get(CONF_CLOUD_TOKEN):
        return token
    if raw := entry.data.get(CONF_HARDWARE_UUID):
        return str(uuid.UUID(bytes=bytes.fromhex(raw)))
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerpalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            PowerpalSecret(coordinator, "pairing_code", _pairing_code),
            PowerpalSecret(coordinator, "hardware_uuid", _hardware_uuid),
        ]
    )


class PowerpalSecret(PowerpalEntity, TextEntity):
    _attr_mode = TextMode.PASSWORD  # shown masked
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_native_min = 0
    _attr_native_max = 64

    def __init__(
        self,
        coordinator: PowerpalCoordinator,
        key: str,
        value: Callable[[ConfigEntry], str | None],
    ) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = key
        self._value = value

    @property
    def available(self) -> bool:
        return True  # these come from the config entry, not from a poll

    @property
    def native_value(self) -> str | None:
        return self._value(self.coordinator.config_entry)

    async def async_set_value(self, value: str) -> None:
        raise ServiceValidationError("This value is read-only")
