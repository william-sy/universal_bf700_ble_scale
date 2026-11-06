"""Config flow and sub-entry flow tests."""

from __future__ import annotations

from datetime import date

from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.universal_ble_scale.config_flow import age_on, birth_date_for_age
from custom_components.universal_ble_scale.const import DOMAIN

from .helpers import ADDR, inject, make_info

PROFILE_INPUT = {"nickname": "bob!", "height": 180, "age": 34, "sex": "male", "activity": "3"}
PERSON = {
    "nickname": "BOB", "uid": "1234", "height": 180, "birth_date": "1990-05-17",
    "sex": "male", "activity": 3,
}


def hub(hass: HomeAssistant, **kw) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="SANITAS SBF70",
        unique_id=ADDR,
        data={"address": ADDR, "name": "SANITAS SBF70"},
        subentries_data=[
            {"data": PERSON, "subentry_type": "person", "title": "BOB",
             "unique_id": "BOB", "subentry_id": "bobid"}
        ],
        **kw,
    )
    entry.add_to_hass(hass)
    return entry


async def test_bluetooth_discovery_creates_scale_with_first_person(
    hass: HomeAssistant, mock_setup_entry
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=make_info()
    )
    assert result["step_id"] == "bluetooth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "profile"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], PROFILE_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"address": ADDR, "name": "SANITAS SBF70"}
    (sub,) = result["subentries"]
    assert sub["data"]["nickname"] == "BOB"
    assert age_on(date.fromisoformat(sub["data"]["birth_date"])) == 34
    assert int(sub["data"]["uid"], 16) > 0
    assert sub["data"]["activity"] == 3


async def test_discovery_aborts_if_configured(hass: HomeAssistant) -> None:
    hub(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=make_info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_user_flow(hass: HomeAssistant, mock_setup_entry) -> None:
    inject(make_info())
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": ADDR})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], PROFILE_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_hides_configured_scale(hass: HomeAssistant) -> None:
    hub(hass)
    inject(make_info())
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_invalid_nickname(hass: HomeAssistant, mock_setup_entry) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=make_info()
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**PROFILE_INPUT, "nickname": "!!!"}
    )
    assert result["errors"] == {"nickname": "invalid_nickname"}


async def test_add_person_subentry(hass: HomeAssistant) -> None:
    entry = hub(hass)
    inject(make_info())
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "person"), context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    dup = await hass.config_entries.subentries.async_configure(
        result["flow_id"], PROFILE_INPUT
    )
    assert dup["errors"] == {"nickname": "nickname_in_use"}
    bad = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**PROFILE_INPUT, "nickname": "?"}
    )
    assert bad["errors"] == {"nickname": "invalid_nickname"}
    done = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**PROFILE_INPUT, "nickname": "ann", "sex": "female"}
    )
    assert done["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert {s.unique_id for s in entry.subentries.values()} == {"BOB", "ANN"}
    assert hass.states.get("sensor.ann_weight") is not None  # reloaded with new person


async def test_reconfigure_person(hass: HomeAssistant) -> None:
    entry = hub(hass)
    inject(make_info())
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "person"),
        context={"source": "reconfigure", "subentry_id": "bobid"},
    )
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"height": 185, "age": 50, "sex": "male", "activity": "4"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    data = entry.subentries["bobid"].data
    assert data["height"] == 185 and data["activity"] == 4
    assert data["uid"] == "1234"
    assert date.fromisoformat(data["birth_date"]).year == date.today().year - 50


async def test_reconfigure_keeps_exact_birth_date_if_age_unchanged(
    hass: HomeAssistant,
) -> None:
    entry = hub(hass)
    inject(make_info())
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    age = age_on(date(1990, 5, 17))
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "person"),
        context={"source": "reconfigure", "subentry_id": "bobid"},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"height": 181, "age": age, "sex": "male", "activity": "3"}
    )
    assert entry.subentries["bobid"].data["birth_date"] == "1990-05-17"


def test_birth_date_helpers() -> None:
    assert birth_date_for_age(30, date(2024, 2, 29)) == date(1994, 2, 28)
    assert age_on(date(1990, 6, 1), date(2026, 5, 31)) == 35
