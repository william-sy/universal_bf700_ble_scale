"""Setup, sessions, scan window, entities, migration, diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.exc import BleakError

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.universal_ble_scale.const import DOMAIN, EVENT_MEASUREMENT
from custom_components.universal_ble_scale.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.universal_ble_scale.protocol import Measurement
from custom_components.universal_ble_scale.session import SessionError, SessionResult

from .helpers import ADDR, inject, make_info
from .test_config_flow import PERSON, hub

M = Measurement(datetime(2026, 11, 6, 8, 0, tzinfo=UTC), 1648, 82.4, 500, 21.5, 55.0, 42.0, 3.0)
M2 = Measurement(datetime(2026, 11, 7, 8, 0, tzinfo=UTC), 1600, 80.0, 500, 21.0, 55.0, 42.0, 3.0)
SESSION = "custom_components.universal_ble_scale.coordinator.ScaleSession"
CONNECT = "custom_components.universal_ble_scale.coordinator.establish_connection"


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = hub(hass)
    inject(make_info())
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _session(result=None, error=None):
    client = MagicMock(disconnect=AsyncMock())
    patcher = patch(SESSION)
    mock = patcher.start()
    mock.return_value.run = AsyncMock(return_value=result, side_effect=error)
    return client, patcher, mock


async def test_setup_unload_unknown_state_and_devices(hass: HomeAssistant) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.bob_weight").state == "unknown"
    devs = dr.async_get(hass)
    scale = devs.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
    assert scale is not None
    person = devs.async_get_device_by_identifier((DOMAIN, "bobid"), entry.entry_id) \
        or devs.async_get_child_device_by_identifier((DOMAIN, "bobid"), entry.entry_id)
    assert person is not None
    assert person.via_device_id == scale.id
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_advertisement_triggers_session_and_updates_sensors(
    hass: HomeAssistant,
) -> None:
    result = SessionResult(battery=90, measurements={"bobid": [M, M2]})
    client, patcher, session = _session(result)
    events = async_capture_events(hass, EVENT_MEASUREMENT)
    try:
        with patch(CONNECT, AsyncMock(return_value=client)):
            entry = await _setup(hass)
            inject(make_info())
            await hass.async_block_till_done(wait_background_tasks=True)
    finally:
        patcher.stop()
    # newest wins, every weigh-in was announced
    assert hass.states.get("sensor.bob_weight").state == "80.0"
    assert hass.states.get("sensor.bob_body_fat").state == "21.0"
    assert hass.states.get("sensor.bob_bmi").state == "24.7"
    assert hass.states.get("sensor.sanitas_sbf70_battery").state == "90"
    assert hass.states.get("sensor.sanitas_sbf70_last_sync").state != "unknown"
    assert [e.data["weight_kg"] for e in events] == [82.4, 80.0]
    assert events[0].data["person"] == "BOB"
    client.disconnect.assert_awaited()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["address"] == "**REDACTED**"
    assert diag["people"][0]["uid"] == "**REDACTED**"
    assert diag["battery"] == 90


async def test_adopted_uid_is_persisted_without_reload(hass: HomeAssistant) -> None:
    result = SessionResult(battery=80, adopted={"bobid": 0xABCD})
    client, patcher, _ = _session(result)
    try:
        with patch(CONNECT, AsyncMock(return_value=client)):
            entry = await _setup(hass)
            coordinator = entry.runtime_data
            inject(make_info())
            await hass.async_block_till_done(wait_background_tasks=True)
    finally:
        patcher.stop()
    assert entry.subentries["bobid"].data["uid"] == "abcd"
    assert entry.runtime_data is coordinator  # not reloaded


async def test_session_failure_is_swallowed(hass: HomeAssistant) -> None:
    client, patcher, _ = _session(error=SessionError("nope"))
    client.disconnect = AsyncMock(side_effect=BleakError("x"))
    try:
        with patch(CONNECT, AsyncMock(return_value=client)):
            await _setup(hass)
            inject(make_info())
            await hass.async_block_till_done(wait_background_tasks=True)
    finally:
        patcher.stop()
    assert hass.states.get("sensor.bob_weight").state == "unknown"


async def test_connect_failure_is_swallowed(hass: HomeAssistant) -> None:
    with patch(CONNECT, AsyncMock(side_effect=BleakError("no"))):
        await _setup(hass)
        inject(make_info())
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.bob_weight").state == "unknown"


async def test_scan_now_opens_window_and_is_patient(hass: HomeAssistant) -> None:
    result = SessionResult(battery=90, measurements={"bobid": [M]})
    client, patcher, session = _session(result)
    try:
        with patch(CONNECT, AsyncMock(return_value=client)):
            await _setup(hass)
            assert hass.states.get("binary_sensor.sanitas_sbf70_scanning").state == "off"
            await hass.services.async_call(
                "number", "set_value",
                {"entity_id": "number.sanitas_sbf70_scan_duration", "value": 30},
                blocking=True,
            )
            await hass.services.async_call(
                "button", "press", {"entity_id": "button.sanitas_sbf70_scan_now"},
                blocking=True,
            )
            await hass.async_block_till_done(wait_background_tasks=True)
            assert hass.states.get("binary_sensor.sanitas_sbf70_scanning").state == "on"
            # scale was already advertising -> straight into a patient session
            kwargs = session.call_args.kwargs
            assert kwargs["measure_timeout"] == 120.0
            assert hass.states.get("sensor.bob_weight").state == "82.4"
            # window closes after the configured 30 minutes
            async_fire_time_changed(hass, datetime.now(UTC).replace(microsecond=0)
                                    + __import__("datetime").timedelta(minutes=31))
            await hass.async_block_till_done()
            assert hass.states.get("binary_sensor.sanitas_sbf70_scanning").state == "off"
    finally:
        patcher.stop()


async def test_scan_duration_default_and_restore(hass: HomeAssistant) -> None:
    await _setup(hass)
    assert hass.states.get("number.sanitas_sbf70_scan_duration").state == "10"


async def test_no_people_means_no_session(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title="SANITAS SBF70", unique_id=ADDR,
        data={"address": ADDR, "name": "SANITAS SBF70"},
    )
    entry.add_to_hass(hass)
    inject(make_info())
    with patch(CONNECT, AsyncMock()) as connect:
        await hass.config_entries.async_setup(entry.entry_id)
        inject(make_info())
        await hass.async_block_till_done(wait_background_tasks=True)
    connect.assert_not_awaited()


async def test_migrate_v1_entry_keeps_entities(hass: HomeAssistant) -> None:
    old = MockConfigEntry(
        domain=DOMAIN, version=1, title="BOB (SANITAS SBF70)", unique_id=f"{ADDR}-BOB",
        data={"address": ADDR, "name": "SANITAS SBF70", **PERSON},
    )
    old.add_to_hass(hass)
    reg = er.async_get(hass)
    reg.async_get_or_create(
        "sensor", DOMAIN, f"{old.entry_id}_weight", config_entry=old,
        suggested_object_id="bob_weight",
    )
    inject(make_info())
    assert await hass.config_entries.async_setup(old.entry_id)
    await hass.async_block_till_done()
    assert old.version == 2
    assert old.unique_id == ADDR
    assert old.title == "SANITAS SBF70"
    (sub,) = old.subentries.values()
    assert sub.data["nickname"] == "BOB" and sub.data["uid"] == "1234"
    ent = reg.async_get("sensor.bob_weight")
    assert ent.unique_id == f"{sub.subentry_id}_weight"
    assert ent.config_subentry_id == sub.subentry_id


async def test_migrate_second_v1_entry_for_same_scale_merges(hass: HomeAssistant) -> None:
    primary = hub(hass)
    other = MockConfigEntry(
        domain=DOMAIN, version=1, title="ANN (SANITAS SBF70)", unique_id=f"{ADDR}-ANN",
        data={"address": ADDR, "name": "SANITAS SBF70",
              **{**PERSON, "nickname": "ANN", "uid": "99"}},
    )
    other.add_to_hass(hass)
    await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()
    assert {s.unique_id for s in primary.subentries.values()} == {"BOB", "ANN"}
    assert hass.config_entries.async_get_entry(other.entry_id) is None
