"""Diagnostics for Universal BLE Scale."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_ADDRESS, CONF_BIRTH_DATE, CONF_NICKNAME, CONF_UID
from .coordinator import ScaleConfigEntry

TO_REDACT = {CONF_ADDRESS, CONF_BIRTH_DATE, CONF_NICKNAME, CONF_UID}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ScaleConfigEntry
) -> dict[str, Any]:
    """Return diagnostics."""
    state = entry.runtime_data.data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "scale_name": entry.runtime_data.scale_name,
        "battery": state.battery,
        "last_measurement": asdict(state.measurement) if state.measurement else None,
    }
