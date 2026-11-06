"""Config flow tests."""

from __future__ import annotations

from datetime import date

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.universal_ble_scale.config_flow import (
    age_on,
    birth_date_for_age,
)
from custom_components.universal_ble_scale.const import DOMAIN

from .helpers import ADDR, inject, make_info

PROFILE_INPUT = {
    "nickname": "bob!",
    "height": 180,
    "age": 34,
    "sex": "male",
    "activity": "3",
}


info = make_info
inject_bluetooth_service_info = lambda hass, i: inject(i)


async def test_bluetooth_discovery(hass: HomeAssistant, mock_setup_entry) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info()
    )
    assert result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "profile"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], PROFILE_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["nickname"] == "BOB"
    assert data["address"] == ADDR
    assert age_on(date.fromisoformat(data["birth_date"])) == 34
    assert int(data["uid"], 16) > 0
    assert data["activity"] == 3


async def test_discovery_aborts_if_configured(hass: HomeAssistant) -> None:
    MockConfigEntry(
        domain=DOMAIN, data={"address": ADDR}, unique_id=f"{ADDR}-BOB"
    ).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow_second_person_same_scale(
    hass: HomeAssistant, mock_setup_entry
) -> None:
    MockConfigEntry(
        domain=DOMAIN, data={"address": ADDR}, unique_id=f"{ADDR}-BOB"
    ).add_to_hass(hass)
    inject_bluetooth_service_info(hass, info())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"address": ADDR}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**PROFILE_INPUT, "nickname": "ann", "sex": "female"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["nickname"] == "ANN"


async def test_duplicate_profile_aborts(hass: HomeAssistant, mock_setup_entry) -> None:
    MockConfigEntry(
        domain=DOMAIN, data={"address": ADDR}, unique_id=f"{ADDR}-BOB"
    ).add_to_hass(hass)
    inject_bluetooth_service_info(hass, info())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"address": ADDR}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], PROFILE_INPUT
    )
    assert result["type"] is FlowResultType.ABORT


async def test_invalid_nickname(hass: HomeAssistant, mock_setup_entry) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=info()
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**PROFILE_INPUT, "nickname": "!!!"}
    )
    assert result["errors"] == {"nickname": "invalid_nickname"}


async def test_user_flow_no_devices(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


def test_birth_date_helpers() -> None:
    assert birth_date_for_age(30, date(2024, 2, 29)) == date(1994, 2, 28)
    assert age_on(date(1990, 6, 1), date(2026, 5, 31)) == 35
