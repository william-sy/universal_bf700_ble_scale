"""Sensors for Universal BLE Scale."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfMass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ScaleConfigEntry, ScaleCoordinator, ScaleState

PARALLEL_UPDATES = 0

type Value = float | int | datetime | None


@dataclass(frozen=True, kw_only=True)
class ScaleSensorDescription(SensorEntityDescription):
    """Sensor description with a value extractor."""

    value_fn: Callable[[ScaleCoordinator, ScaleState], Value]


def _bmi(coordinator: ScaleCoordinator, state: ScaleState) -> float | None:
    if state.measurement is None:
        return None
    height_m = coordinator.profile.height_cm / 100
    return round(state.measurement.weight_kg / height_m**2, 1)


SENSORS: tuple[ScaleSensorDescription, ...] = (
    ScaleSensorDescription(
        key="weight",
        translation_key="weight",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda c, s: s.measurement.weight_kg if s.measurement else None,
    ),
    ScaleSensorDescription(
        key="body_fat",
        translation_key="body_fat",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c, s: s.measurement.body_fat if s.measurement else None,
    ),
    ScaleSensorDescription(
        key="body_water",
        translation_key="body_water",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c, s: s.measurement.body_water if s.measurement else None,
    ),
    ScaleSensorDescription(
        key="muscle",
        translation_key="muscle",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda c, s: s.measurement.muscle if s.measurement else None,
    ),
    ScaleSensorDescription(
        key="bone_mass",
        translation_key="bone_mass",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda c, s: s.measurement.bone_kg if s.measurement else None,
    ),
    ScaleSensorDescription(
        key="bmi",
        translation_key="bmi",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_bmi,
    ),
    ScaleSensorDescription(
        key="impedance",
        translation_key="impedance",
        native_unit_of_measurement="Ω",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c, s: s.measurement.impedance if s.measurement else None,
    ),
    ScaleSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c, s: s.battery,
    ),
    ScaleSensorDescription(
        key="last_measurement",
        translation_key="last_measurement",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c, s: s.measurement.timestamp if s.measurement else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    async_add_entities(ScaleSensor(coordinator, d) for d in SENSORS)


class ScaleSensor(CoordinatorEntity[ScaleCoordinator], RestoreSensor):
    """A scale sensor. The scale is asleep most of the time, so state is restored."""

    _attr_has_entity_name = True
    entity_description: ScaleSensorDescription

    def __init__(
        self, coordinator: ScaleCoordinator, description: ScaleSensorDescription
    ) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            connections={("bluetooth", coordinator.address)},
            name=entry.title,
            manufacturer="Beurer / Sanitas",
            model=coordinator.scale_name,
        )
        self._restored: Value = None

    async def async_added_to_hass(self) -> None:
        """Restore the last value."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._restored = last.native_value  # type: ignore[assignment]

    @property
    def available(self) -> bool:
        """Always available: no data just means nobody stepped on the scale yet."""
        return True

    @property
    def native_value(self) -> Value:
        """Return the value, or the restored one until the source has data."""
        data = self.coordinator.data
        has_source = (
            data.battery is not None
            if self.entity_description.key == "battery"
            else data.measurement is not None
        )
        if not has_source:
            return self._restored
        return self.entity_description.value_fn(self.coordinator, data)
