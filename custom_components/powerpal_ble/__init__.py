"""Powerpal (BLE) integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.start import async_at_started

from .coordinator import PowerpalCoordinator

PLATFORMS = [Platform.BUTTON, Platform.NUMBER, Platform.SENSOR, Platform.TEXT]

type PowerpalConfigEntry = ConfigEntry[PowerpalCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: PowerpalConfigEntry) -> bool:
    coordinator = PowerpalCoordinator(hass, entry)
    await coordinator.async_load()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Never wait for the device here: a connect over a weak link can take over a minute and
    # would hold up Home Assistant's startup. Poll once HA has finished starting instead.
    async def _first_poll(_: HomeAssistant) -> None:
        entry.async_create_background_task(
            hass, coordinator.async_refresh(), name="powerpal first poll"
        )

    entry.async_on_unload(async_at_started(hass, _first_poll))

    # Reload when the options change (not on other entry updates such as the saved credential).
    async def _options_updated(hass: HomeAssistant, entry: PowerpalConfigEntry) -> None:
        if entry.runtime_data.options_changed:
            await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_options_updated))

    # Saves are throttled, so write the log out when Home Assistant stops or the entry unloads.
    async def _flush(_: object = None) -> None:
        await coordinator.async_flush()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _flush))
    entry.async_on_unload(_flush)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PowerpalConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
