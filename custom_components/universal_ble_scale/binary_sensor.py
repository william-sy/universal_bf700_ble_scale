"""Binary sensor: is a scan window open?"""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import ScaleConfigEntry, ScaleCoordinator
from .entity import scale_device

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the scanning sensor."""
    async_add_entities([ScanningBinarySensor(entry.runtime_data)])


class ScanningBinarySensor(CoordinatorEntity[ScaleCoordinator], BinarySensorEntity):
    """On while HA is actively waiting for the scale."""

    _attr_has_entity_name = True
    _attr_translation_key = "scanning"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: ScaleCoordinator) -> None:
        """Initialise."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_scanning"
        self._attr_device_info = scale_device(coordinator)

    @property
    def is_on(self) -> bool:
        """Return True while a scan window is open."""
        return self.coordinator.scanning
