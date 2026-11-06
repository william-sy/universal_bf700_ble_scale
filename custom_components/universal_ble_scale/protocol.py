"""Wire protocol for the Beurer / Sanitas "BF700 family" of BLE scales.

Covers Sanitas SBF70, Silvercrest SBF75, Beurer BF700 / BF710 / BF800 and the
Runtastic Libra. Pure Python (no Home Assistant imports) so it can be unit
tested in isolation.

References:
  * openScale ``BeurerSanitasHandler`` (GPL-3.0-or-later)
  * danielfaust/bt-scale (GPL-3.0)

Framing
-------
Everything runs over one GATT characteristic (0xFFE1 in service 0xFFE0), used
for both writes and notifications. Every frame starts with a "start byte":

    0xE7  Sanitas SBF70/SBF75, Beurer BF710
    0xF7  Beurer BF700/BF800, Runtastic Libra

``[start][cmd][payload...]`` is a normal frame. A few "alternative start"
frames replace the low nibble of the start byte (0x6 init, 0x9 set time,
0xA disconnect).

The scale computes body composition itself from the user profile stored on the
scale, so the profile (height, birth date, sex, activity) has to be written to
the scale; Home Assistant only decodes the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import struct

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

START_E7 = 0xE7
START_F7 = 0xF7

NIBBLE_INIT = 0x6
NIBBLE_SET_TIME = 0x9
NIBBLE_DISCONNECT = 0xA

CMD_USER_ADD = 0x31
CMD_USER_DELETE = 0x32
CMD_USER_LIST = 0x33
CMD_USER_INFO = 0x34  # scale -> app
CMD_USER_UPDATE = 0x35
CMD_USER_DETAILS = 0x36
CMD_DO_MEASUREMENT = 0x40
CMD_GET_SAVED = 0x41
CMD_SAVED_MEASUREMENT = 0x42  # scale -> app
CMD_DELETE_SAVED = 0x43
CMD_SET_USER_WEIGHT = 0x44  # sets the reference weight used to recognise the user
CMD_GET_USER_WEIGHT = 0x45
CMD_GET_UNKNOWN = 0x46
CMD_UNKNOWN_INFO = 0x47  # scale -> app
CMD_ASSIGN_UNKNOWN = 0x4B
CMD_UNKNOWN_LIVE = 0x4C  # scale -> app (weigh-in nobody owns, body comp included)
CMD_SET_UNIT = 0x4D
CMD_SCALE_STATUS = 0x4F
CMD_LIVE_WEIGHT = 0x58  # scale -> app
CMD_MEASUREMENT = 0x59  # scale -> app
CMD_SCALE_ACK = 0xF0  # scale -> app
CMD_APP_ACK = 0xF1  # app -> scale

UNIT_KG = 0x01

# Record layout (verified against the official Sanitas Health Coach app): a
# measurement is split over two frames of 15 bytes, each contributing the 11
# bytes at [4:15]. Anything after byte 14 is padding and must not be joined.
PAYLOAD_SLICE = slice(4, 15)

# Weight is transmitted as a big-endian uint16 in units of 50 g.
WEIGHT_STEP_KG = 0.05

# --- The "stuck at 12.80 kg" bug -------------------------------------------
# 12.80 kg == raw 0x0100 == 256. Two plausible origins, both guarded against:
#   1. Live 0x58 frames are [start][0x58][flag][w_hi][w_lo]. flag is 0x01 while
#      the scale is still settling and w is 0, so a client that reads the weight
#      from the wrong offset (u16 at [2]) sees exactly 0x0100 until the scale
#      stabilises and then often never updates.
#   2. A client that takes the first live frame as the result instead of
#      waiting for flag == 0 / the full 0x59 record.
# We therefore (a) read the weight at the right offset, (b) only trust live
# frames with flag == 0, (c) prefer the full record and (d) reject a raw value
# of exactly 0x0100 when the record has no impedance (i.e. it is a placeholder
# rather than a real weigh-in).
STUCK_RAW_WEIGHT = 0x0100

MIN_WEIGHT_KG = 2.0
MAX_WEIGHT_KG = 300.0

_BF700_NAME_HINTS = ("bf-700", "bf-800", "bf800", "libra-b", "libra-w", "libra")


def start_byte_candidates(name: str | None) -> tuple[int, int]:
    """Return start bytes to try, most likely first, based on the BLE name."""
    lowered = (name or "").lower()
    if any(hint in lowered for hint in _BF700_NAME_HINTS):
        return (START_F7, START_E7)
    return (START_E7, START_F7)


def alt_start(start: int, nibble: int) -> int:
    """Return the alternative start byte for the given low nibble."""
    return (start & 0xF0) | nibble


def encode_uid(uid: int | None) -> bytes:
    """Encode a user id as 8 big-endian bytes (all zeros for 'no user')."""
    return (uid or 0).to_bytes(8, "big")


def build_command(start: int, cmd: int, payload: bytes = b"") -> bytes:
    """Build a normal ``[start][cmd][payload]`` frame."""
    return bytes([start, cmd]) + payload


def build_init(start: int) -> bytes:
    """Build the init frame."""
    return bytes([alt_start(start, NIBBLE_INIT), 0x01])


def build_set_time(start: int, when: datetime) -> bytes:
    """Build the set-time frame (4-byte big-endian Unix time)."""
    return bytes([alt_start(start, NIBBLE_SET_TIME)]) + int(when.timestamp()).to_bytes(
        4, "big"
    )


def build_app_ack(start: int, frame: bytes) -> bytes:
    """Acknowledge a multi-part frame by echoing bytes 1..3 of it."""
    return bytes([start, CMD_APP_ACK]) + frame[1:4]


def is_scale_ack(frame: bytes, start: int, cmd: int) -> bool:
    """Return True if ``frame`` is the scale's 0xF0 ack for ``cmd``."""
    return (
        len(frame) >= 4
        and frame[0] == start
        and frame[1] == CMD_SCALE_ACK
        and frame[2] == cmd
    )


# --- user profile -----------------------------------------------------------


def normalize_nickname(name: str) -> str:
    """Normalise a nickname to what the scale can store (A-Z, 0-9, max 3)."""
    return "".join(c for c in name.upper() if c.isascii() and c.isalnum())[:3]


@dataclass(frozen=True, slots=True)
class UserProfile:
    """Local description of the person the scale should measure."""

    nickname: str
    height_cm: int
    birth_date: date
    male: bool
    activity: int  # 1 (none) .. 5 (extreme athlete)

    def payload(self, uid: int) -> bytes:
        """Return the USER_ADD / USER_UPDATE payload."""
        nick = normalize_nickname(self.nickname).encode("ascii")
        nick = nick[:3].ljust(3, b"\x00")
        return (
            encode_uid(uid)
            + nick
            + bytes(
                [
                    self.birth_date.year - 1900,
                    self.birth_date.month - 1,
                    self.birth_date.day,
                    self.height_cm,
                    (0x80 if self.male else 0x00) | (self.activity & 0x0F),
                ]
            )
        )


@dataclass(frozen=True, slots=True)
class RemoteUser:
    """A user slot as reported by the scale."""

    index: int
    count: int
    uid: int
    name: str
    birth_year: int


def parse_user_info(frame: bytes) -> RemoteUser | None:
    """Parse a 0x34 USER_INFO frame."""
    if len(frame) < 16 or frame[1] != CMD_USER_INFO:
        return None
    return RemoteUser(
        index=frame[3],
        count=frame[2],
        uid=int.from_bytes(frame[4:12], "big"),
        name=frame[12:15].split(b"\x00")[0].decode("ascii", "ignore"),
        birth_year=1900 + frame[15],
    )


def remote_matches_profile(remote: RemoteUser, profile: UserProfile) -> bool:
    """Return True if a scale user looks like the given local profile."""
    nick = normalize_nickname(profile.nickname)
    return (
        bool(nick)
        and normalize_nickname(remote.name).startswith(nick[:3])
        and remote.birth_year == profile.birth_date.year
    )


@dataclass(frozen=True, slots=True)
class RemoteUserDetails:
    """Details of a user as stored on the scale (0xF0 0x36 reply)."""

    name: str
    birth_year: int
    birth_month: int
    birth_day: int
    height_cm: int
    male: bool
    activity: int


def parse_user_details(frame: bytes) -> RemoteUserDetails | None:
    """Parse the scale's reply to USER_DETAILS."""
    if len(frame) < 12 or frame[1] != CMD_SCALE_ACK or frame[2] != CMD_USER_DETAILS:
        return None
    if frame[3] != 0:
        return None
    flags = frame[11]
    return RemoteUserDetails(
        name=frame[4:7].split(b"\x00")[0].decode("ascii", "ignore"),
        birth_year=1900 + frame[7],
        birth_month=frame[8] + 1,
        birth_day=frame[9],
        height_cm=frame[10],
        male=bool(flags & 0x80),
        activity=flags & 0x0F,
    )


def details_match_profile(details: RemoteUserDetails, profile: UserProfile) -> bool:
    """Return True if the scale already stores this profile."""
    return (
        details.height_cm == profile.height_cm
        and details.birth_year == profile.birth_date.year
        and details.birth_month == profile.birth_date.month
        and details.birth_day == profile.birth_date.day
        and details.male == profile.male
        and details.activity == profile.activity
    )


# --- status -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScaleStatus:
    """Reply to SCALE_STATUS."""

    battery: int
    unit: int
    firmware: int | None


def parse_scale_status(frame: bytes) -> ScaleStatus | None:
    """Parse ``e7 f0 4f <st> <batt> <wthr> <fthr> <unit> <..> <..> <..> <fw>``."""
    if len(frame) < 8 or frame[1] != CMD_SCALE_ACK or frame[2] != CMD_SCALE_STATUS:
        return None
    return ScaleStatus(
        battery=min(frame[4], 100),
        unit=frame[7],
        firmware=frame[11] if len(frame) > 11 else None,
    )


# --- measurements -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Measurement:
    """One decoded weigh-in."""

    timestamp: datetime
    raw_weight: int
    weight_kg: float
    impedance: int | None
    body_fat: float | None
    body_water: float | None
    muscle: float | None
    bone_kg: float | None

    @property
    def has_body_composition(self) -> bool:
        """Return True if the scale produced body composition values."""
        return any(
            v is not None
            for v in (self.body_fat, self.body_water, self.muscle, self.bone_kg)
        )

    @property
    def is_valid(self) -> bool:
        """Return True if this looks like a real weigh-in (see 12.80 kg note)."""
        if not MIN_WEIGHT_KG <= self.weight_kg <= MAX_WEIGHT_KG:
            return False
        return not (
            self.raw_weight == STUCK_RAW_WEIGHT
            and not self.impedance
            and not self.has_body_composition
        )


def _opt(value: int, divisor: float) -> float | None:
    return round(value / divisor, 2) if value else None


def decode_measurement(buf: bytes, *, now: datetime | None = None) -> Measurement | None:
    """Decode the 16-byte measurement record."""
    if len(buf) < 16:
        return None
    ts, raw_weight, impedance, fat, water, muscle, bone = struct.unpack(
        ">IHHHHHH", buf[:16]
    )
    ts &= 0x7FFFFFFF  # bit 31 is set by some firmwares (openScale #1500)
    now = now or datetime.now(UTC)
    try:
        when = datetime.fromtimestamp(ts, UTC)
    except (OverflowError, OSError, ValueError):
        when = now
    # A scale whose clock was never set reports 1970-ish dates; use "now".
    if when.year < 2020 or when > now.replace(year=now.year + 1):
        when = now
    return Measurement(
        timestamp=when,
        raw_weight=raw_weight,
        weight_kg=round(raw_weight * WEIGHT_STEP_KG, 2),
        impedance=impedance or None,
        body_fat=_opt(fat, 10),
        body_water=_opt(water, 10),
        muscle=_opt(muscle, 10),
        bone_kg=round(bone * WEIGHT_STEP_KG, 2) if bone else None,
    )


@dataclass(frozen=True, slots=True)
class LiveWeight:
    """A 0x58 live weight frame."""

    stable: bool
    raw: int

    @property
    def weight_kg(self) -> float:
        """Return the weight in kg."""
        return round(self.raw * WEIGHT_STEP_KG, 2)

    @property
    def trustworthy(self) -> bool:
        """Only stable, non-placeholder frames count."""
        return self.stable and self.raw not in (0, STUCK_RAW_WEIGHT)


def parse_live_weight(frame: bytes) -> LiveWeight | None:
    """Parse ``[start][58][flag][w_hi][w_lo]`` (flag 0 == stable)."""
    if len(frame) < 5 or frame[1] != CMD_LIVE_WEIGHT:
        return None
    return LiveWeight(stable=frame[2] == 0, raw=int.from_bytes(frame[3:5], "big"))


class RecordAssembler:
    """Reassemble multi-frame measurement records.

    Saved records (0x42): frame ``[start][42][count][current]...`` where an odd
    ``current`` starts a record and an even one completes it.

    Live records (0x59): part 1 carries the owner uid at bytes 5..12, parts 2
    and 3 carry data; complete at ``current == count``.

    Unowned live records (0x4C): like 0x59 but without a uid, two parts.
    """

    def __init__(self) -> None:
        """Initialise empty buffers."""
        self._saved = b""
        self._live = b""
        self._unknown = b""
        self.live_uid: int | None = None

    def feed_saved(self, frame: bytes) -> bytes | None:
        """Feed a 0x42 frame; return the full buffer when a record completes."""
        if len(frame) < 5:
            return None
        if frame[3] % 2:
            self._saved = bytes(frame[PAYLOAD_SLICE])
            return None
        buf = self._saved + bytes(frame[PAYLOAD_SLICE])
        self._saved = b""
        return buf

    def feed_live(self, frame: bytes) -> tuple[int | None, bytes | None]:
        """Feed a 0x59 frame; return ``(uid, buffer)`` when a record completes."""
        if len(frame) < 5:
            return None, None
        count, current = frame[2], frame[3]
        if current == 1:
            self.live_uid = int.from_bytes(frame[5:13], "big")
            self._live = b""
            return None, None
        payload = bytes(frame[PAYLOAD_SLICE])
        self._live = payload if current == 2 else self._live + payload
        if current >= count:
            buf, self._live = self._live, b""
            return self.live_uid, buf
        return None, None

    def feed_unknown(self, frame: bytes) -> bytes | None:
        """Feed a 0x4C frame; return the buffer when the record completes."""
        if len(frame) < 5:
            return None
        count, current = frame[2], frame[3]
        payload = bytes(frame[PAYLOAD_SLICE])
        self._unknown = payload if current == 1 else self._unknown + payload
        if current >= count:
            buf, self._unknown = self._unknown, b""
            return buf
        return None


@dataclass(frozen=True, slots=True)
class UnknownMeasurement:
    """A weigh-in the scale could not attribute to a user (0x47 frame)."""

    ident: int
    timestamp_raw: int
    raw_weight: int
    impedance: int

    @property
    def epoch(self) -> int:
        """Return the Unix timestamp."""
        return self.timestamp_raw & 0x7FFFFFFF


def parse_unknown_info(frame: bytes) -> UnknownMeasurement | None:
    """Parse ``[start][47][count][cur][id][ts4][w2][imp2]``."""
    if len(frame) < 13 or frame[1] != CMD_UNKNOWN_INFO:
        return None
    return UnknownMeasurement(
        ident=frame[4],
        timestamp_raw=int.from_bytes(frame[5:9], "big"),
        raw_weight=int.from_bytes(frame[9:11], "big"),
        impedance=int.from_bytes(frame[11:13], "big"),
    )


def build_assign_unknown(start: int, uid: int, unknown: UnknownMeasurement) -> bytes:
    """Build ``[start][4B][uid8][ts4][w2][imp2][id]``."""
    return build_command(
        start,
        CMD_ASSIGN_UNKNOWN,
        encode_uid(uid)
        + unknown.timestamp_raw.to_bytes(4, "big")
        + unknown.raw_weight.to_bytes(2, "big")
        + unknown.impedance.to_bytes(2, "big")
        + bytes([unknown.ident]),
    )


def build_set_user_weight(
    start: int, uid: int, raw_weight: int, fat_tenths: int, timestamp_raw: int
) -> bytes:
    """Build ``[start][44][uid8][w2][fat2][ts4]`` (sets the reference weight)."""
    return build_command(
        start,
        CMD_SET_USER_WEIGHT,
        encode_uid(uid)
        + raw_weight.to_bytes(2, "big")
        + fat_tenths.to_bytes(2, "big")
        + timestamp_raw.to_bytes(4, "big"),
    )


def has_reference_weight(frame: bytes) -> bool:
    """Parse the reply to 0x45: a short reply means no reference weight yet."""
    return (
        len(frame) >= 12
        and frame[1] == CMD_SCALE_ACK
        and frame[2] == CMD_GET_USER_WEIGHT
        and int.from_bytes(frame[8:10], "big") > 0
    )


def weight_only_measurement(unknown: UnknownMeasurement) -> Measurement:
    """Build a measurement from the little the 0x47 list gives us."""
    return Measurement(
        timestamp=datetime.fromtimestamp(unknown.epoch, UTC),
        raw_weight=unknown.raw_weight,
        weight_kg=round(unknown.raw_weight * WEIGHT_STEP_KG, 2),
        impedance=unknown.impedance or None,
        body_fat=None,
        body_water=None,
        muscle=None,
        bone_kg=None,
    )
