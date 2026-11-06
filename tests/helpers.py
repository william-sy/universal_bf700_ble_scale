"""Bluetooth test helpers (HA's own live in core's tests/, not shipped to PyPI)."""

from __future__ import annotations

import time

from bleak.backends.scanner import AdvertisementData, BLEDevice
from habluetooth import BluetoothServiceInfoBleak, get_manager

ADDR = "AA:BB:CC:DD:EE:FF"


def generate_ble_device(address: str, name: str) -> BLEDevice:
    return BLEDevice(address, name, {})


def generate_advertisement_data(local_name: str) -> AdvertisementData:
    return AdvertisementData(
        local_name=local_name,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        rssi=-60,
        tx_power=-127,
        platform_data=(),
    )


def make_info(name: str = "SANITAS SBF70", address: str = ADDR) -> BluetoothServiceInfoBleak:
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[],
        source="local",
        device=generate_ble_device(address, name),
        advertisement=generate_advertisement_data(name),
        time=time.monotonic(),
        connectable=True,
        tx_power=None,
    )


def inject(info: BluetoothServiceInfoBleak) -> None:
    """Feed an advertisement into the bluetooth manager."""
    get_manager().scanner_adv_received(info)
