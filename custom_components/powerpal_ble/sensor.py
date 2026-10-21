from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PowerpalConfigEntry
from .coordinator import PowerpalCoordinator, PowerpalData
from .entity import PowerpalEntity


@dataclass(frozen=True, kw_only=True)
class PowerpalSensorDescription(SensorEntityDescription):
    value_fn: Callable[[PowerpalData], float | int | None]


SENSORS = (
    PowerpalSensorDescription(
        key="power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: round(d.watts, 1),
    ),
    PowerpalSensorDescription(
        key="energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value_fn=lambda d: d.total_kwh,
    ),
    PowerpalSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.state.battery,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerpalConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(PowerpalSensor(coordinator, desc) for desc in SENSORS)


class PowerpalSensor(PowerpalEntity, SensorEntity):
    entity_description: PowerpalSensorDescription

    def __init__(
        self, coordinator: PowerpalCoordinator, description: PowerpalSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
