"""Constants for Universal BLE Scale."""

from __future__ import annotations

DOMAIN = "universal_ble_scale"

CONF_ADDRESS = "address"
CONF_NICKNAME = "nickname"
CONF_HEIGHT = "height"
CONF_AGE = "age"
CONF_BIRTH_DATE = "birth_date"
CONF_SEX = "sex"
CONF_ACTIVITY = "activity"
CONF_UID = "uid"

SEX_MALE = "male"
SEX_FEMALE = "female"

DEFAULT_HEIGHT = 175
DEFAULT_AGE = 35
DEFAULT_ACTIVITY = 3

# After a session, ignore advertisements for this long. The scale keeps
# advertising for a while after it disconnects and reconnecting in a loop would
# drain its batteries.
SESSION_COOLDOWN = 45.0
