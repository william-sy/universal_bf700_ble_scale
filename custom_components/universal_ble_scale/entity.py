"""Shared entity helpers."""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .coordinator import ScaleCoordinator


def scale_device(coordinator: ScaleCoordinator) -> DeviceInfo:
    """Device info of the scale itself."""
    entry = coordinator.config_entry
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        connections={("bluetooth", coordinator.address)},
        name=entry.title,
        manufacturer="Beurer / Sanitas",
        model=coordinator.scale_name,
    )


def person_device(coordinator: ScaleCoordinator, subentry_id: str, name: str) -> DeviceInfo:
    """Device info of one person (child of the scale)."""
    entry_id = coordinator.config_entry.entry_id
    parent = dr.async_get(coordinator.hass).async_get_device_by_identifier(
        (DOMAIN, entry_id), entry_id
    )
    info = DeviceInfo(
        identifiers={(DOMAIN, subentry_id)},
        name=name,
        manufacturer="Beurer / Sanitas",
        model="Body scale profile",
    )
    if parent is not None:
        info["via_device_id"] = parent.id
    return info
