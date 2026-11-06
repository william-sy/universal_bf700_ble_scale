"""Button: scan now."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ScaleConfigEntry, ScaleCoordinator
from .entity import scale_device

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the button."""
    async_add_entities([ScanNowButton(entry.runtime_data)])


class ScanNowButton(ButtonEntity):
    """Open a scan window: HA waits patiently for the scale and connects."""

    _attr_has_entity_name = True
    _attr_translation_key = "scan_now"

    def __init__(self, coordinator: ScaleCoordinator) -> None:
        """Initialise."""
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_scan_now"
        self._attr_device_info = scale_device(coordinator)

    async def async_press(self) -> None:
        """Open the scan window."""
        self._coordinator.async_arm()
