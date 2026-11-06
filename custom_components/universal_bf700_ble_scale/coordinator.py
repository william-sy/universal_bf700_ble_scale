"""Coordinator: one per scale. Waits for it to advertise, then runs one session."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    ARMED_COOLDOWN,
    ARMED_MAX_WAIT,
    CONF_ACTIVITY,
    CONF_ADDRESS,
    CONF_ADOPTED,
    CONF_BIRTH_DATE,
    CONF_HEIGHT,
    CONF_NICKNAME,
    CONF_PENDING_DELETE,
    CONF_SEX,
    CONF_UID,
    DEFAULT_SCAN_MINUTES,
    DOMAIN,
    EVENT_MEASUREMENT,
    SESSION_COOLDOWN,
    SEX_MALE,
    SUBENTRY_PERSON,
)
from .protocol import Measurement, RemoteUser, UserProfile
from .session import Person, ScaleSession, SessionError

_LOGGER = logging.getLogger(__name__)

type ScaleConfigEntry = ConfigEntry[ScaleCoordinator]


@dataclass(frozen=True, slots=True)
class ScaleState:
    """Latest known state of the scale."""

    battery: int | None = None
    last_sync: datetime | None = None
    measurements: Mapping[str, Measurement] = field(default_factory=dict)
    users: tuple[RemoteUser, ...] | None = None  # slots on the scale, once synced


def profile_from_data(data: Mapping[str, Any]) -> UserProfile:
    """Build the protocol profile from stored person data."""
    return UserProfile(
        nickname=data[CONF_NICKNAME],
        height_cm=int(data[CONF_HEIGHT]),
        birth_date=date.fromisoformat(data[CONF_BIRTH_DATE]),
        male=data[CONF_SEX] == SEX_MALE,
        activity=int(data[CONF_ACTIVITY]),
    )


def people_signature(entry: ConfigEntry) -> frozenset[tuple[str, Any]]:
    """Everything about the people that matters except the (auto-adopted) uid."""
    return frozenset(
        (sid, tuple(sorted((k, str(v)) for k, v in sub.data.items() if k != CONF_UID)))
        for sid, sub in entry.subentries.items()
        if sub.subentry_type == SUBENTRY_PERSON
    )


class ScaleCoordinator(DataUpdateCoordinator[ScaleState]):
    """Push-style coordinator driven by BLE advertisements."""

    config_entry: ScaleConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ScaleConfigEntry) -> None:
        """Initialise."""
        super().__init__(hass, _LOGGER, config_entry=entry, name=entry.title)
        self.address: str = entry.data[CONF_ADDRESS]
        self.scale_name: str | None = entry.data.get("name")
        self.data = ScaleState()
        self.signature = people_signature(entry)
        self.known = self.build_known()
        self.scan_minutes = DEFAULT_SCAN_MINUTES
        self._armed_until = 0.0
        self._unarm: CALLBACK_TYPE | None = None
        self._next_allowed = 0.0
        self._task: asyncio.Task[None] | None = None
        locks: dict[str, asyncio.Lock] = hass.data.setdefault(DOMAIN, {})
        self._lock = locks.setdefault(self.address, asyncio.Lock())  # type: ignore[arg-type]

    # -- people ------------------------------------------------------------

    def person_subentries(self) -> dict[str, ConfigSubentry]:
        """Return the person sub-entries."""
        return {
            sid: sub
            for sid, sub in self.config_entry.subentries.items()
            if sub.subentry_type == SUBENTRY_PERSON
        }

    def people(self) -> list[Person]:
        """Return the people to measure."""
        return [
            Person(sid, int(sub.data[CONF_UID], 16), profile_from_data(sub.data))
            for sid, sub in self.person_subentries().items()
        ]

    def build_known(self) -> dict[str, tuple[int, bool]]:
        """Map person -> (uid, adopted from another app)."""
        return {
            sid: (int(sub.data[CONF_UID], 16), bool(sub.data.get(CONF_ADOPTED)))
            for sid, sub in self.person_subentries().items()
        }

    def pending_delete(self) -> list[int]:
        """Return the uids queued for deletion from the scale."""
        return [int(u, 16) for u in self.config_entry.data.get(CONF_PENDING_DELETE, [])]

    @callback
    def async_queue_delete(self, uid: int) -> None:
        """Queue a scale slot for deletion at the next sync."""
        queued = {f"{u:x}" for u in self.pending_delete()} | {f"{uid:x}"}
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_PENDING_DELETE: sorted(queued)},
        )
        self.async_update_listeners()

    def height_cm(self, subentry_id: str) -> int:
        """Return a person's height."""
        return int(self.config_entry.subentries[subentry_id].data[CONF_HEIGHT])

    # -- scan window -------------------------------------------------------

    @property
    def scanning(self) -> bool:
        """Return True while a scan window is open."""
        return time.monotonic() < self._armed_until

    @callback
    def async_arm(self) -> None:
        """Open a scan window ("I'm about to weigh")."""
        seconds = self.scan_minutes * 60
        self._armed_until = time.monotonic() + seconds
        if self._unarm:
            self._unarm()
        self._unarm = async_call_later(self.hass, seconds + 0.5, self._async_disarm)
        self._next_allowed = 0.0
        self.async_update_listeners()
        # If the scale is already awake, go straight in.
        last = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
        if last is not None and time.monotonic() - last.time < 15:
            self._async_maybe_start(last.name)

    @callback
    def _async_disarm(self, _now: datetime | None = None) -> None:
        self._unarm = None
        self._armed_until = 0.0
        self.async_update_listeners()

    @callback
    def async_start(self) -> None:
        """Start listening for the scale."""
        self.config_entry.async_on_unload(
            bluetooth.async_register_callback(
                self.hass,
                self._async_on_advertisement,
                bluetooth.BluetoothCallbackMatcher(address=self.address, connectable=True),
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )
        self.config_entry.async_on_unload(self._async_cancel)

    @callback
    def _async_cancel(self) -> None:
        if self._task:
            self._task.cancel()
        if self._unarm:
            self._unarm()
            self._unarm = None

    # -- sessions ----------------------------------------------------------

    @callback
    def _async_on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """The scale woke up (someone stepped on it)."""
        self._async_maybe_start(service_info.name)

    @callback
    def _async_maybe_start(self, name: str | None) -> None:
        if self._task and not self._task.done():
            return
        if time.monotonic() < self._next_allowed or not self.person_subentries():
            return
        self.scale_name = name or self.scale_name
        self._task = self.config_entry.async_create_background_task(
            self.hass, self._async_run_session(), f"{DOMAIN}_{self.address}_session"
        )

    async def _async_run_session(self) -> None:
        try:
            async with self._lock:
                await self._async_session()
        finally:
            cooldown = ARMED_COOLDOWN if self.scanning else SESSION_COOLDOWN
            self._next_allowed = time.monotonic() + cooldown

    def _ble_device(self):  # noqa: ANN202
        return bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)

    async def _async_session(self) -> None:
        if (device := self._ble_device()) is None:
            _LOGGER.debug("%s: no connectable path to scale", self.address)
            return
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                device,
                self.config_entry.title,
                max_attempts=3,
                ble_device_callback=self._ble_device,
            )
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: connect failed: %s", self.address, err)
            return

        kwargs: dict[str, float] = {}
        if self.scanning:
            kwargs["measure_timeout"] = ARMED_MAX_WAIT
        try:
            result = await ScaleSession(
                client,
                self.scale_name,
                self.people(),
                delete_uids=self.pending_delete(),
                **kwargs,
            ).run()
        except (SessionError, BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: session failed: %s", self.address, err)
            return
        finally:
            try:
                await client.disconnect()
            except (BleakError, TimeoutError):
                _LOGGER.debug("%s: disconnect failed", self.address)

        for key, uid in result.adopted.items():
            if (sub := self.config_entry.subentries.get(key)) is not None:
                self.hass.config_entries.async_update_subentry(
                    self.config_entry,
                    sub,
                    data={**sub.data, CONF_UID: f"{uid:x}", CONF_ADOPTED: True},
                )

        self.known = self.build_known()
        if result.deleted:
            remaining = [
                u
                for u in self.config_entry.data.get(CONF_PENDING_DELETE, [])
                if int(u, 16) not in result.deleted
            ]
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_PENDING_DELETE: remaining},
            )

        latest = dict(self.data.measurements)
        for key, measurements in result.measurements.items():
            sub = self.config_entry.subentries.get(key)
            if sub is None:
                continue
            for m in measurements:
                previous = latest.get(key)
                if previous is not None and m.timestamp <= previous.timestamp:
                    continue  # duplicate
                latest[key] = m
                self.hass.bus.async_fire(EVENT_MEASUREMENT, _event_data(self, sub, m))
        self.async_set_updated_data(
            replace(
                self.data,
                battery=result.battery if result.battery is not None else self.data.battery,
                last_sync=datetime.now(UTC),
                users=tuple(result.users),
                measurements=latest,
            )
        )


def _event_data(
    coordinator: ScaleCoordinator, sub: ConfigSubentry, m: Measurement
) -> dict[str, Any]:
    return {
        "address": coordinator.address,
        "person": sub.data[CONF_NICKNAME],
        "subentry_id": sub.subentry_id,
        "timestamp": m.timestamp.isoformat(),
        "weight_kg": m.weight_kg,
        "body_fat": m.body_fat,
        "body_water": m.body_water,
        "muscle": m.muscle,
        "bone_kg": m.bone_kg,
        "impedance": m.impedance,
    }
