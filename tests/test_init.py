"""Setup, session trigger, sensors and reconfigure."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.exc import BleakError

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.universal_ble_scale.const import DOMAIN
from custom_components.universal_ble_scale.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.universal_ble_scale.protocol import Measurement
from custom_components.universal_ble_scale.session import SessionError, SessionResult

from .helpers import ADDR, inject, make_info as info

inject_bluetooth_service_info = lambda hass, i: inject(i)

DATA = {
    "address": ADDR,
    "name": "SANITAS SBF70",
    "nickname": "BOB",
    "uid": "1234",
    "height": 180,
    "birth_date": "1990-05-17",
    "sex": "male",
    "activity": 3,
}
M = Measurement(datetime(2026, 11, 6, 8, 0, tzinfo=UTC), 1648, 82.4, 500, 21.5, 55.0, 42.0, 3.0)
SESSION = "custom_components.universal_ble_scale.coordinator.ScaleSession"
CONNECT = "custom_components.universal_ble_scale.coordinator.establish_connection"


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=DATA, title="BOB", unique_id=f"{ADDR}-BOB")
    entry.add_to_hass(hass)
    inject_bluetooth_service_info(hass, info())
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_unload_and_unknown_state(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.bob_weight").state == "unknown"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_advertisement_triggers_session_and_updates_sensors(
    hass: HomeAssistant,
) -> None:
    client = MagicMock(disconnect=AsyncMock())
    result = SessionResult(uid=0x1234, battery=90, measurement=M)
    with (
        patch(CONNECT, AsyncMock(return_value=client)),
        patch(SESSION) as session,
    ):
        session.return_value.run = AsyncMock(return_value=result)
        entry = await _setup(hass)
        inject_bluetooth_service_info(hass, info())
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.bob_weight").state == "82.4"
    assert hass.states.get("sensor.bob_body_fat").state == "21.5"
    assert hass.states.get("sensor.bob_body_water").state == "55.0"
    assert hass.states.get("sensor.bob_muscle").state == "42.0"
    assert hass.states.get("sensor.bob_bone_mass").state == "3.0"
    assert hass.states.get("sensor.bob_bmi").state == "25.4"
    client.disconnect.assert_awaited()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["uid"] == "**REDACTED**"
    assert diag["battery"] == 90


async def test_adopted_uid_is_persisted(hass: HomeAssistant) -> None:
    client = MagicMock(disconnect=AsyncMock())
    result = SessionResult(uid=0xABCD, uid_adopted=True, battery=80)
    with patch(CONNECT, AsyncMock(return_value=client)), patch(SESSION) as session:
        session.return_value.run = AsyncMock(return_value=result)
        entry = await _setup(hass)
        inject_bluetooth_service_info(hass, info())
        await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.data["uid"] == "abcd"


async def test_session_failure_is_swallowed(hass: HomeAssistant) -> None:
    client = MagicMock(disconnect=AsyncMock(side_effect=BleakError("x")))
    with patch(CONNECT, AsyncMock(return_value=client)), patch(SESSION) as session:
        session.return_value.run = AsyncMock(side_effect=SessionError("nope"))
        await _setup(hass)
        inject_bluetooth_service_info(hass, info())
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.bob_weight").state == "unknown"


async def test_connect_failure_is_swallowed(hass: HomeAssistant) -> None:
    with patch(CONNECT, AsyncMock(side_effect=BleakError("no"))):
        await _setup(hass)
        inject_bluetooth_service_info(hass, info())
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.bob_weight").state == "unknown"


async def test_reconfigure_updates_profile(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    with patch("custom_components.universal_ble_scale.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"height": 185, "age": 50, "sex": "male", "activity": "4"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["height"] == 185
    assert entry.data["activity"] == 4
    assert date.fromisoformat(entry.data["birth_date"]).year == date.today().year - 50
