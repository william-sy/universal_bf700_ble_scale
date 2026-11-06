"""Coordinator: waits for the scale to advertise, then runs one session."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import date
import logging
import time

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_ACTIVITY,
    CONF_ADDRESS,
    CONF_BIRTH_DATE,
    CONF_HEIGHT,
    CONF_NICKNAME,
    CONF_SEX,
    CONF_UID,
    DOMAIN,
    SESSION_COOLDOWN,
    SEX_MALE,
)
from .protocol import Measurement, UserProfile
from .session import ScaleSession, SessionError

_LOGGER = logging.getLogger(__name__)

type ScaleConfigEntry = ConfigEntry[ScaleCoordinator]


@dataclass(frozen=True, slots=True)
class ScaleState:
    """Latest known state of the scale."""

    measurement: Measurement | None = None
    battery: int | None = None


def profile_from_entry(entry: ConfigEntry) -> UserProfile:
    """Build the protocol profile from stored config data."""
    data = entry.data
    return UserProfile(
        nickname=data[CONF_NICKNAME],
        height_cm=int(data[CONF_HEIGHT]),
        birth_date=date.fromisoformat(data[CONF_BIRTH_DATE]),
        male=data[CONF_SEX] == SEX_MALE,
        activity=int(data[CONF_ACTIVITY]),
    )


class ScaleCoordinator(DataUpdateCoordinator[ScaleState]):
    """Push-style coordinator driven by BLE advertisements."""

    config_entry: ScaleConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ScaleConfigEntry) -> None:
        """Initialise."""
        super().__init__(hass, _LOGGER, config_entry=entry, name=entry.title)
        self.address: str = entry.data[CONF_ADDRESS]
        self.scale_name: str | None = entry.data.get("name")
        self.profile = profile_from_entry(entry)
        self.uid = int(entry.data[CONF_UID], 16)
        self.data = ScaleState()
        self._next_allowed = 0.0
        self._task: asyncio.Task[None] | None = None
        locks: dict[str, asyncio.Lock] = hass.data.setdefault(DOMAIN, {})
        self._lock = locks.setdefault(self.address, asyncio.Lock())  # type: ignore[arg-type]

    @callback
    def async_start(self) -> None:
        """Start listening for the scale."""
        self.config_entry.async_on_unload(
            bluetooth.async_register_callback(
                self.hass,
                self._async_on_advertisement,
                bluetooth.BluetoothCallbackMatcher(
                    address=self.address, connectable=True
                ),
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )
        self.config_entry.async_on_unload(self._async_cancel)

    @callback
    def _async_cancel(self) -> None:
        if self._task:
            self._task.cancel()

    @callback
    def _async_on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """The scale woke up (someone stepped on it)."""
        if self._task and not self._task.done():
            return
        if time.monotonic() < self._next_allowed:
            return
        self.scale_name = service_info.name or self.scale_name
        self._task = self.config_entry.async_create_background_task(
            self.hass, self._async_run_session(), f"{DOMAIN}_{self.address}_session"
        )

    async def _async_run_session(self) -> None:
        try:
            async with self._lock:
                await self._async_session()
        finally:
            self._next_allowed = time.monotonic() + SESSION_COOLDOWN

    def _ble_device(self):  # noqa: ANN202
        return bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )

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

        try:
            result = await ScaleSession(
                client,
                self.scale_name,
                self.profile,
                self.uid,
            ).run()
        except (SessionError, BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: session failed: %s", self.address, err)
            return
        finally:
            try:
                await client.disconnect()
            except (BleakError, TimeoutError):
                _LOGGER.debug("%s: disconnect failed", self.address)

        if result.uid_adopted:
            self.uid = result.uid
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_UID: f"{result.uid:x}"},
            )
        measurement = result.measurement
        previous = self.data.measurement
        if previous and measurement and measurement.timestamp <= previous.timestamp:
            measurement = None  # duplicate
        self.async_set_updated_data(
            replace(
                self.data,
                battery=result.battery if result.battery is not None else self.data.battery,
                measurement=measurement or self.data.measurement,
            )
        )
