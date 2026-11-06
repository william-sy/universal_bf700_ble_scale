"""Constants for Universal BF700 BLE Scale."""

from __future__ import annotations

DOMAIN = "universal_bf700_ble_scale"

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

SUBENTRY_PERSON = "person"

# While a scan window is open ("Scan now") HA is patient: short cooldown and a
# long wait for the weigh-in after the scale wakes.
ARMED_COOLDOWN = 5.0
ARMED_MAX_WAIT = 120.0
DEFAULT_SCAN_MINUTES = 10

EVENT_MEASUREMENT = f"{DOMAIN}_measurement"

CONF_ADOPTED = "adopted"
CONF_PENDING_DELETE = "pending_delete"
