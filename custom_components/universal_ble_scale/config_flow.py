"""Config flow for Universal BLE Scale."""

from __future__ import annotations

from datetime import date
import secrets
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryData,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_ACTIVITY,
    CONF_ADDRESS,
    CONF_AGE,
    CONF_BIRTH_DATE,
    CONF_HEIGHT,
    CONF_NICKNAME,
    CONF_SEX,
    CONF_UID,
    DEFAULT_ACTIVITY,
    DEFAULT_AGE,
    DEFAULT_HEIGHT,
    DOMAIN,
    SEX_FEMALE,
    SEX_MALE,
    SUBENTRY_PERSON,
)
from .protocol import normalize_nickname

_NAME_HINTS = ("sbf7", "sanitas", "silvercrest", "bf700", "bf710", "bf-7", "bf-8",
               "bf800", "beurer", "libra", "aicdscale")


def _looks_like_scale(info: BluetoothServiceInfoBleak) -> bool:
    return any(hint in (info.name or "").lower() for hint in _NAME_HINTS)


def birth_date_for_age(age: int, today: date | None = None) -> date:
    """Return the birth date that makes someone ``age`` years old today."""
    today = today or date.today()
    try:
        return today.replace(year=today.year - age)
    except ValueError:  # 29 Feb
        return today.replace(year=today.year - age, day=28)


def age_on(birth: date, today: date | None = None) -> int:
    """Return the age of someone born on ``birth``."""
    today = today or date.today()
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def _profile_schema(defaults: dict[str, Any], *, with_nickname: bool) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if with_nickname:
        fields[vol.Required(CONF_NICKNAME, default=defaults.get(CONF_NICKNAME, ""))] = (
            TextSelector()
        )
    fields.update(
        {
            vol.Required(CONF_HEIGHT, default=defaults.get(CONF_HEIGHT, DEFAULT_HEIGHT)): NumberSelector(
                NumberSelectorConfig(min=100, max=220, step=1, unit_of_measurement="cm",
                                     mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_AGE, default=defaults.get(CONF_AGE, DEFAULT_AGE)): NumberSelector(
                NumberSelectorConfig(min=10, max=100, step=1, mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_SEX, default=defaults.get(CONF_SEX, SEX_MALE)): SelectSelector(
                SelectSelectorConfig(
                    options=[SEX_MALE, SEX_FEMALE],
                    translation_key="sex",
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_ACTIVITY, default=str(defaults.get(CONF_ACTIVITY, DEFAULT_ACTIVITY))
            ): SelectSelector(
                SelectSelectorConfig(
                    options=["1", "2", "3", "4", "5"],
                    translation_key="activity",
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )
    return vol.Schema(fields)


def _person_data(user_input: dict[str, Any], nickname: str) -> dict[str, Any]:
    """Build the stored person data from the profile form."""
    return {
        CONF_NICKNAME: nickname,
        CONF_UID: f"{secrets.randbits(31) | 0x100:x}",
        CONF_HEIGHT: int(user_input[CONF_HEIGHT]),
        CONF_BIRTH_DATE: birth_date_for_age(int(user_input[CONF_AGE])).isoformat(),
        CONF_SEX: user_input[CONF_SEX],
        CONF_ACTIVITY: int(user_input[CONF_ACTIVITY]),
    }


class UniversalBleScaleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a scale (and its first person)."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialise."""
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._devices: dict[str, BluetoothServiceInfoBleak] = {}

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Each person on the scale is a sub-entry."""
        return {SUBENTRY_PERSON: PersonSubentryFlow}

    # -- discovery ---------------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a discovered scale."""
        assert self._discovery is not None
        if user_input is not None:
            return await self.async_step_profile()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._discovery.name},
        )

    # -- manual ------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a scale from the Bluetooth devices HA can see."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            self._discovery = self._devices[address]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_profile()

        configured = {e.unique_id for e in self._async_current_entries()}
        infos = [
            i
            for i in async_discovered_service_info(self.hass, True)
            if i.name and i.address not in configured
        ]
        likely = [i for i in infos if _looks_like_scale(i)]
        self._devices = {i.address: i for i in (likely or infos)}
        if not self._devices:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=a, label=f"{i.name} ({a})")
                                for a, i in self._devices.items()
                            ]
                        )
                    )
                }
            ),
        )

    # -- first person ------------------------------------------------------

    async def async_step_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the first person's profile."""
        assert self._discovery is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            nickname = normalize_nickname(user_input[CONF_NICKNAME])
            if not nickname:
                errors[CONF_NICKNAME] = "invalid_nickname"
            else:
                return self.async_create_entry(
                    title=self._discovery.name,
                    data={CONF_ADDRESS: self._discovery.address, "name": self._discovery.name},
                    subentries=[
                        ConfigSubentryData(
                            data=_person_data(user_input, nickname),
                            subentry_type=SUBENTRY_PERSON,
                            title=nickname,
                            unique_id=nickname,
                        )
                    ],
                )
        return self.async_show_form(
            step_id="profile",
            data_schema=_profile_schema(user_input or {}, with_nickname=True),
            errors=errors,
            description_placeholders={"name": self._discovery.name},
        )


class PersonSubentryFlow(ConfigSubentryFlow):
    """Add or edit a person on the scale."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a person."""
        errors: dict[str, str] = {}
        if user_input is not None:
            nickname = normalize_nickname(user_input[CONF_NICKNAME])
            taken = {s.unique_id for s in self._get_entry().subentries.values()}
            if not nickname:
                errors[CONF_NICKNAME] = "invalid_nickname"
            elif nickname in taken:
                errors[CONF_NICKNAME] = "nickname_in_use"
            else:
                return self.async_create_entry(
                    title=nickname,
                    data=_person_data(user_input, nickname),
                    unique_id=nickname,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_profile_schema(user_input or {}, with_nickname=True),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change height, age, sex or activity; pushed to the scale on next use."""
        subentry = self._get_reconfigure_subentry()
        birth = date.fromisoformat(subentry.data[CONF_BIRTH_DATE])
        if user_input is not None:
            new_age = int(user_input[CONF_AGE])
            new_birth = birth if new_age == age_on(birth) else birth_date_for_age(new_age)
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                data_updates={
                    CONF_HEIGHT: int(user_input[CONF_HEIGHT]),
                    CONF_BIRTH_DATE: new_birth.isoformat(),
                    CONF_SEX: user_input[CONF_SEX],
                    CONF_ACTIVITY: int(user_input[CONF_ACTIVITY]),
                },
            )
        defaults = {
            CONF_HEIGHT: subentry.data[CONF_HEIGHT],
            CONF_AGE: age_on(birth),
            CONF_SEX: subentry.data[CONF_SEX],
            CONF_ACTIVITY: subentry.data[CONF_ACTIVITY],
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_profile_schema(defaults, with_nickname=False),
        )
