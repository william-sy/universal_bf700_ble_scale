"""Diagnostics for Universal BF700 BLE Scale."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ADDRESS,
    CONF_BIRTH_DATE,
    CONF_NICKNAME,
    CONF_PENDING_DELETE,
    CONF_UID,
)
from .coordinator import ScaleConfigEntry

TO_REDACT = {CONF_ADDRESS, CONF_BIRTH_DATE, CONF_NICKNAME, CONF_PENDING_DELETE, CONF_UID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ScaleConfigEntry
) -> dict[str, Any]:
    """Return diagnostics."""
    coordinator = entry.runtime_data
    state = coordinator.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "scale_name": coordinator.scale_name,
        "scanning": coordinator.scanning,
        "battery": state.battery,
        "last_sync": state.last_sync.isoformat() if state.last_sync else None,
        "people": [
            async_redact_data(dict(sub.data), TO_REDACT)
            for sub in coordinator.person_subentries().values()
        ],
        "last_measurements": {sid: asdict(m) for sid, m in state.measurements.items()},
    }
