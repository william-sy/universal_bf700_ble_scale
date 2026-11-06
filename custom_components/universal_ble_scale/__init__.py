"""Universal BLE Scale: Beurer / Sanitas / Silvercrest BLE scales via HA Bluetooth."""

from __future__ import annotations

import logging
from types import MappingProxyType

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_ACTIVITY,
    CONF_ADDRESS,
    CONF_BIRTH_DATE,
    CONF_HEIGHT,
    CONF_NICKNAME,
    CONF_SEX,
    CONF_UID,
    DOMAIN,
    SUBENTRY_PERSON,
)
from .coordinator import ScaleConfigEntry, ScaleCoordinator, people_signature

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
]

_PERSON_KEYS = (CONF_NICKNAME, CONF_UID, CONF_HEIGHT, CONF_BIRTH_DATE, CONF_SEX, CONF_ACTIVITY)


async def async_migrate_entry(hass: HomeAssistant, entry: ScaleConfigEntry) -> bool:
    """Migrate v1 (one entry per person) to v2 (one entry per scale + people)."""
    if entry.version > 2:
        return False
    if entry.version == 1:
        person = {k: entry.data[k] for k in _PERSON_KEYS if k in entry.data}
        address = entry.data[CONF_ADDRESS]
        subentry = ConfigSubentry(
            data=MappingProxyType(person),
            subentry_type=SUBENTRY_PERSON,
            title=person[CONF_NICKNAME],
            unique_id=person[CONF_NICKNAME],
        )
        # Several v1 entries for one scale collapse into the first one we see.
        primary = next(
            (
                e
                for e in hass.config_entries.async_entries(DOMAIN)
                if e.entry_id != entry.entry_id
                and e.version == 2
                and e.data.get(CONF_ADDRESS) == address
            ),
            None,
        )
        if primary is not None:
            if subentry.unique_id not in {s.unique_id for s in primary.subentries.values()}:
                hass.config_entries.async_add_subentry(primary, subentry)
            _move_entities(hass, entry, primary, person)
            hass.async_create_task(hass.config_entries.async_remove(entry.entry_id))
            return False
        hass.config_entries.async_add_subentry(entry, subentry)
        hass.config_entries.async_update_entry(
            entry,
            data={CONF_ADDRESS: address, "name": entry.data.get("name")},
            title=entry.data.get("name") or address,
            unique_id=address,
            version=2,
        )
        _move_entities(hass, entry, entry, person, subentry)
    return True


def _move_entities(
    hass: HomeAssistant,
    old: ScaleConfigEntry,
    new: ScaleConfigEntry,
    person: dict,
    subentry: ConfigSubentry | None = None,
) -> None:
    """Keep entity ids/history: re-key the old per-person entities."""
    if subentry is None:
        subentry = next(
            s for s in new.subentries.values() if s.unique_id == person[CONF_NICKNAME]
        )
    reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(reg, old.entry_id):
        prefix = f"{old.entry_id}_"
        if not (ent.unique_id or "").startswith(prefix):
            continue
        key = ent.unique_id[len(prefix) :]
        if key in ("battery",):
            continue  # becomes a scale-level entity (same key, scale device)
        reg.async_update_entity(
            ent.entity_id,
            new_unique_id=f"{subentry.subentry_id}_{key}",
            config_entry_id=new.entry_id,
            config_subentry_id=subentry.subentry_id,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ScaleConfigEntry) -> bool:
    """Set up a scale from a config entry."""
    coordinator = ScaleCoordinator(hass, entry)
    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start()
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: ScaleConfigEntry) -> None:
    """Reload when people are added/removed/edited (not when a uid is adopted)."""
    coordinator = entry.runtime_data
    if people_signature(entry) != coordinator.signature:
        coordinator.signature = people_signature(entry)  # avoid re-entering
        # A person was removed: free the slot HA created for them (but never a
        # slot adopted from another app, which may still be in use there).
        for sid, (uid, adopted) in coordinator.known.items():
            if sid not in entry.subentries and not adopted:
                coordinator.async_queue_delete(uid)
        await hass.config_entries.async_reload(entry.entry_id)
    else:
        coordinator.known = coordinator.build_known()


async def async_unload_entry(hass: HomeAssistant, entry: ScaleConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
