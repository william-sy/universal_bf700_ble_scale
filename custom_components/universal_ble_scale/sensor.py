"""Sensors for Universal BLE Scale."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import voluptuous as vol
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
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_NICKNAME, DOMAIN
from .coordinator import ScaleConfigEntry, ScaleCoordinator, ScaleState
from .entity import person_device, scale_device
from .protocol import Measurement

PARALLEL_UPDATES = 0

type Value = float | int | datetime | None


@dataclass(frozen=True, kw_only=True)
class PersonSensorDescription(SensorEntityDescription):
    """Per-person sensor description."""

    value_fn: Callable[[Measurement, int], Value]


@dataclass(frozen=True, kw_only=True)
class ScaleSensorDescription(SensorEntityDescription):
    """Scale-level sensor description."""

    value_fn: Callable[[ScaleState], Value]


PERSON_SENSORS: tuple[PersonSensorDescription, ...] = (
    PersonSensorDescription(
        key="weight",
        translation_key="weight",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda m, h: m.weight_kg,
    ),
    PersonSensorDescription(
        key="body_fat",
        translation_key="body_fat",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda m, h: m.body_fat,
    ),
    PersonSensorDescription(
        key="body_water",
        translation_key="body_water",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda m, h: m.body_water,
    ),
    PersonSensorDescription(
        key="muscle",
        translation_key="muscle",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda m, h: m.muscle,
    ),
    PersonSensorDescription(
        key="bone_mass",
        translation_key="bone_mass",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda m, h: m.bone_kg,
    ),
    PersonSensorDescription(
        key="bmi",
        translation_key="bmi",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda m, h: round(m.weight_kg / (h / 100) ** 2, 1),
    ),
    PersonSensorDescription(
        key="impedance",
        translation_key="impedance",
        native_unit_of_measurement="Ω",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda m, h: m.impedance,
    ),
    PersonSensorDescription(
        key="last_measurement",
        translation_key="last_measurement",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda m, h: m.timestamp,
    ),
)

SCALE_SENSORS: tuple[ScaleSensorDescription, ...] = (
    ScaleSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.battery,
    ),
    ScaleSensorDescription(
        key="last_sync",
        translation_key="last_sync",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.last_sync,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    async_add_entities(ScaleLevelSensor(coordinator, d) for d in SCALE_SENSORS)
    async_add_entities([ScaleUsersSensor(coordinator)])
    entity_platform.async_get_current_platform().async_register_entity_service(
        "delete_scale_user",
        {vol.Required("uid"): cv.string},
        "async_delete_scale_user",
    )
    for sid, sub in coordinator.person_subentries().items():
        async_add_entities(
            (
                PersonSensor(coordinator, sid, sub.data[CONF_NICKNAME], d)
                for d in PERSON_SENSORS
            ),
            config_subentry_id=sid,
        )


class _RestoringSensor(CoordinatorEntity[ScaleCoordinator], RestoreSensor):
    """The scale sleeps most of the time, so restore the last value."""

    _attr_has_entity_name = True
    _restored: Value = None

    async def async_added_to_hass(self) -> None:
        """Restore the last value."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            self._restored = last.native_value  # type: ignore[assignment]

    @property
    def available(self) -> bool:
        """Always available: no data just means nobody stepped on the scale yet."""
        return True


class ScaleLevelSensor(_RestoringSensor):
    """Battery, last sync."""

    entity_description: ScaleSensorDescription

    def __init__(
        self, coordinator: ScaleCoordinator, description: ScaleSensorDescription
    ) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{description.key}"
        self._attr_device_info = scale_device(coordinator)

    @property
    def native_value(self) -> Value:
        """Return the value or the restored one."""
        value = self.entity_description.value_fn(self.coordinator.data)
        return self._restored if value is None else value


class PersonSensor(_RestoringSensor):
    """One person's measurement value."""

    entity_description: PersonSensorDescription

    def __init__(
        self,
        coordinator: ScaleCoordinator,
        subentry_id: str,
        name: str,
        description: PersonSensorDescription,
    ) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self.entity_description = description
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{subentry_id}_{description.key}"
        self._attr_device_info = person_device(coordinator, subentry_id, name)

    @property
    def native_value(self) -> Value:
        """Return the value, or the restored one until there is a measurement."""
        measurement = self.coordinator.data.measurements.get(self._subentry_id)
        if measurement is None:
            return self._restored
        return self.entity_description.value_fn(
            measurement, self.coordinator.height_cm(self._subentry_id)
        )


class ScaleUsersSensor(_RestoringSensor):
    """How many user slots the scale holds, and who they are."""

    _attr_translation_key = "scale_users"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ScaleCoordinator) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_scale_users"
        self._attr_device_info = scale_device(coordinator)

    @property
    def native_value(self) -> Value:
        """Return the number of slots in use (restored until the first sync)."""
        users = self.coordinator.data.users
        return self._restored if users is None else len(users)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """List every slot; ``in_home_assistant`` marks the ones we manage."""
        coordinator = self.coordinator
        mine = {uid for uid, _ in coordinator.known.values()}
        queued = set(coordinator.pending_delete())
        return {
            "users": [
                {
                    "slot": u.index,
                    "name": u.name,
                    "birth_year": u.birth_year,
                    "uid": f"{u.uid:x}",
                    "in_home_assistant": u.uid in mine,
                    "delete_queued": u.uid in queued,
                }
                for u in (coordinator.data.users or ())
            ],
            "pending_delete": sorted(f"{u:x}" for u in queued),
        }

    async def async_delete_scale_user(self, uid: str) -> None:
        """Queue a slot for deletion; it happens the next time the scale wakes."""
        try:
            value = int(uid.strip().lower().removeprefix("0x"), 16)
        except ValueError as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_uid"
            ) from err
        if value in {u for u, _ in self.coordinator.known.values()}:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="uid_in_use",
                translation_placeholders={"uid": uid},
            )
        self.coordinator.async_queue_delete(value)
