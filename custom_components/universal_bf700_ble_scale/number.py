"""Number: how long a scan window stays open."""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEFAULT_SCAN_MINUTES
from .coordinator import ScaleConfigEntry, ScaleCoordinator
from .entity import scale_device

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the number."""
    async_add_entities([ScanDurationNumber(entry.runtime_data)])


class ScanDurationNumber(RestoreNumber):
    """Scan window length in minutes."""

    _attr_has_entity_name = True
    _attr_translation_key = "scan_duration"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 1
    _attr_native_max_value = 180
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: ScaleCoordinator) -> None:
        """Initialise."""
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_scan_duration"
        self._attr_device_info = scale_device(coordinator)
        self._attr_native_value = DEFAULT_SCAN_MINUTES

    async def async_added_to_hass(self) -> None:
        """Restore the last duration."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_number_data()) and last.native_value:
            self._attr_native_value = last.native_value
        self._coordinator.scan_minutes = int(self._attr_native_value or DEFAULT_SCAN_MINUTES)

    async def async_set_native_value(self, value: float) -> None:
        """Set the duration."""
        self._attr_native_value = value
        self._coordinator.scan_minutes = int(value)
        self.async_write_ha_state()
