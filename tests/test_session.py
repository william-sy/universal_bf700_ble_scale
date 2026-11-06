"""Protocol + session tests against a simulated scale (no Home Assistant needed)."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
import struct

import pytest

from custom_components.universal_ble_scale.protocol import (
    CHAR_UUID,
    Measurement,
    UserProfile,
    decode_measurement,
    parse_live_weight,
    start_byte_candidates,
)
from custom_components.universal_ble_scale.session import (
    Person,
    ScaleSession,
    SessionError,
)

UID = 0x1234
PROFILE = UserProfile("Bob", 180, date(1990, 5, 17), True, 3)
PERSON = Person("bob", UID, PROFILE)
ANN = Person("ann", 0x99, UserProfile("Ann", 165, date(1985, 1, 2), False, 2))
NOW = int(datetime.now(UTC).timestamp())


def record(weight_kg: float, *, imp=500, fat=215, water=550, muscle=420, bone=60,
           ts=NOW) -> bytes:
    """22-byte record: ts, weight, imp, fat, water, muscle, bone, bmr, amr, bmi."""
    return struct.pack(">IHHHHHH", ts, round(weight_kg / 0.05), imp, fat, water, muscle,
                       bone) + bytes(6)


def frames_for_record(start: int, cmd: int, rec: bytes, uid: int | None = None):
    """Split a record into two 15-byte frames (11 payload bytes each) like the scale."""
    if cmd == 0x42:
        return [
            bytes([start, 0x42, 2, 1]) + rec[:11],
            bytes([start, 0x42, 2, 2]) + rec[11:22],
        ]
    if cmd == 0x4C:
        return [
            bytes([start, 0x4C, 2, 1]) + rec[:11],
            bytes([start, 0x4C, 2, 2]) + rec[11:22],
        ]
    return [
        bytes([start, 0x59, 3, 1, 0]) + uid.to_bytes(8, "big") + bytes(2),
        bytes([start, 0x59, 3, 2]) + rec[:11],
        bytes([start, 0x59, 3, 3]) + rec[11:22],
    ]


class FakeScale:
    """Minimal scale simulator speaking the E7 or F7 dialect."""

    def __init__(self, start=0xE7, users=(), saved=(), live=None, speaks=None,
                 unknown=(), reference=False):
        self.start = start
        self.speaks = speaks or start
        self.users = list(users)  # (uid, name, year)
        self.saved = list(saved)
        self.live = live  # list of frames sent after DO_MEASUREMENT
        self.push_live = False  # push them without being asked
        self.written: list[bytes] = []
        self.cb = None
        self.unknown = list(unknown)  # (id, ts, raw_weight, impedance)
        self.reference = reference
        self.assigned = None
        self.set_weight = None

    async def start_notify(self, char, cb):
        assert char == CHAR_UUID
        self.cb = cb

    async def stop_notify(self, char):
        self.cb = None

    def _send(self, *frames):
        for f in frames:
            asyncio.get_running_loop().call_soon(self.cb, None, bytearray(f))

    async def write_gatt_char(self, char, data, response):
        self.written.append(bytes(data))
        s = self.speaks
        if data[0] != s and data[0] != (s & 0xF0) | 6 and data[0] != (s & 0xF0) | 9:
            return  # wrong dialect: silence
        if data[0] == (s & 0xF0) | 6:
            self._send(bytes([data[0], 0x00, 0x20]))
            return
        if data[0] == (s & 0xF0) | 9:
            return
        cmd = data[1]
        if cmd == 0x4F:
            self._send(bytes([s, 0xF0, 0x4F, 1, 94, 20, 220, 1, 1, 1, 1, 5]))
        elif cmd == 0x33:
            self._send(bytes([s, 0xF0, 0x33, 0, len(self.users), 8]))
            for i, (uid, name, year) in enumerate(self.users, 1):
                n = name.encode().ljust(3, b"\0")
                self._send(bytes([s, 0x34, len(self.users), i]) + uid.to_bytes(8, "big") + n + bytes([year - 1900]))
        elif cmd == 0x31:
            uid = int.from_bytes(data[2:10], "big")
            self.users.append((uid, data[10:13].rstrip(b"\0").decode(), 1900 + data[13]))
            self._send(bytes([s, 0xF0, 0x31, 0]))
        elif cmd == 0x36:
            self._send(bytes([s, 0xF0, 0x36, 0, *b"BOB", 90, 4, 17, 180, 0x83, 0, 0]))
        elif cmd == 0x35:
            self._send(bytes([s, 0xF0, 0x35, 0]))
        elif cmd == 0x41:
            self._send(bytes([s, 0xF0, 0x41, 0, len(self.saved) * 2]))
            for rec in self.saved:
                self._send(*frames_for_record(s, 0x42, rec))
        elif cmd == 0x43:
            self.saved.clear()
            self._send(bytes([s, 0xF0, 0x43, 0]))
        elif cmd == 0x45:
            if self.reference:
                self._send(bytes([s, 0xF0, 0x45, 0]) + NOW.to_bytes(4, "big")
                           + (1300).to_bytes(2, "big") + (200).to_bytes(2, "big"))
            else:
                self._send(bytes([s, 0xF0, 0x45, 1]))
        elif cmd == 0x46:
            if not self.unknown:
                self._send(bytes([s, 0xF0, 0x46, 1]))
            else:
                self._send(bytes([s, 0xF0, 0x46, 0]))
                for i, (ident, ts, w, imp) in enumerate(self.unknown, 1):
                    self._send(bytes([s, 0x47, len(self.unknown), i, ident])
                               + ts.to_bytes(4, "big") + w.to_bytes(2, "big")
                               + imp.to_bytes(2, "big"))
            if self.push_live and self.live:
                self._send(*self.live)
        elif cmd == 0x4B:
            self.assigned = bytes(data)
            self.unknown.clear()
            self._send(bytes([s, 0xF0, 0x4B, 0]))
        elif cmd == 0x44:
            self.set_weight = bytes(data)
            self.reference = True
            self._send(bytes([s, 0xF0, 0x44, 0]))
        elif cmd == 0x40:
            self._send(bytes([s, 0xF0, 0x40, 0]))
            if self.live:
                self._send(*self.live)
        # app acks (0xF1) need no reply


def session(scale: FakeScale, name="SANITAS SBF70", people=None, **kw):
    return ScaleSession(scale, name, people or [PERSON], measure_timeout=1.0,
                        step_timeout=1.0, **kw)


def best(result, key="bob"):
    found = result.measurements.get(key)
    return found[-1] if found else None


async def test_live_measurement_existing_user():
    live = [
        bytes([0xE7, 0x58, 1, 0, 0]),  # settling: the 12.80 trap
        bytes([0xE7, 0x58, 0, 0x05, 0x14]),
        *frames_for_record(0xE7, 0x59, record(82.4), UID),
    ]
    scale = FakeScale(users=[(UID, "BOB", 1990)], live=live)
    result = await session(scale).run()
    assert best(result).weight_kg == 82.4
    assert best(result).body_fat == 21.5
    assert best(result).body_water == 55.0
    assert best(result).muscle == 42.0
    assert best(result).bone_kg == 3.0
    assert result.battery == 94
    assert not result.adopted
    assert not any(w[1] == 0x31 for w in scale.written if len(w) > 1)  # no user add


async def test_saved_measurement_then_deleted():
    scale = FakeScale(users=[(UID, "BOB", 1990)], saved=[record(70.0)])
    result = await session(scale).run()
    assert best(result).weight_kg == 70.0
    assert scale.saved == []
    assert not any(w[1:2] == b"\x40" for w in scale.written)  # no live measure needed


async def test_adds_unknown_user():
    scale = FakeScale(live=frames_for_record(0xE7, 0x59, record(60.0), UID))
    result = await session(scale).run()
    assert scale.users and scale.users[0][0] == UID
    assert best(result).weight_kg == 60.0


async def test_adopts_user_created_by_other_app():
    scale = FakeScale(users=[(0xABCD, "BOB", 1990)],
                      live=frames_for_record(0xE7, 0x59, record(61.0), 0xABCD))
    result = await session(scale).run()
    assert result.adopted == {"bob": 0xABCD}
    assert best(result).weight_kg == 61.0


async def test_other_users_measurement_ignored():
    scale = FakeScale(users=[(UID, "BOB", 1990)],
                      live=frames_for_record(0xE7, 0x59, record(95.0), 0x9999))
    result = await session(scale).run()
    assert best(result) is None


async def test_f7_dialect_autodetected_despite_name():
    scale = FakeScale(start=0xF7, users=[(UID, "BOB", 1990)],
                      live=frames_for_record(0xF7, 0x59, record(75.0), UID))
    # Name hints E7 first; session must fall back to F7.
    result = await session(scale, name="Some Scale").run()
    assert best(result).weight_kg == 75.0


async def test_stuck_12_80_placeholder_is_discarded():
    rec = record(12.8, imp=0, fat=0, water=0, muscle=0, bone=0)
    scale = FakeScale(users=[(UID, "BOB", 1990)],
                      live=frames_for_record(0xE7, 0x59, rec, UID))
    result = await session(scale).run()
    assert best(result) is None
    assert result.discarded


async def test_real_12_80_with_impedance_is_kept():
    m = decode_measurement(record(12.8, imp=600))
    assert m.is_valid


async def test_silent_scale_raises():
    scale = FakeScale(speaks=0x17)
    with pytest.raises(SessionError):
        await session(scale).run()


def test_live_weight_offset_and_flag():
    # Reading u16 at [2] (the bug) would give 0x0100 == 12.80 kg here.
    unstable = parse_live_weight(bytes([0xE7, 0x58, 1, 0, 0]))
    assert unstable.raw == 0 and not unstable.trustworthy
    stable = parse_live_weight(bytes([0xE7, 0x58, 0, 0x05, 0x14]))
    assert stable.weight_kg == 65.0 and stable.trustworthy
    assert parse_live_weight(bytes([0xE7, 0x58, 0, 0x01, 0x00])).trustworthy is False


def test_timestamp_bit31_and_unset_clock():
    now = datetime.now(UTC)
    m = decode_measurement(record(80.0, ts=NOW | 0x80000000), now=now)
    assert abs((m.timestamp - now).total_seconds()) < 120
    m = decode_measurement(record(80.0, ts=100), now=now)
    assert m.timestamp == now


def test_profile_payload_layout():
    p = PROFILE.payload(UID)
    assert p[:8] == UID.to_bytes(8, "big")
    assert p[8:11] == b"BOB"
    assert p[11:] == bytes([90, 4, 17, 180, 0x83])


def test_start_byte_hints():
    assert start_byte_candidates("SANITAS SBF70")[0] == 0xE7
    assert start_byte_candidates("BF-800")[0] == 0xF7


async def test_first_weighin_before_profile_is_adopted():
    """The scale stored the weigh-in as 'unknown'; assign it and teach the scale."""
    scale = FakeScale(users=[(UID, "BOB", 1990)], unknown=[(7, NOW - 30, 1300, 500)])
    result = await session(scale).run()
    assert best(result).weight_kg == 65.0
    assert best(result).impedance == 500
    # [E7][4B][uid8][ts4][w2][imp2][id]
    assert scale.assigned[:2] == b"\xe7\x4b"
    assert int.from_bytes(scale.assigned[2:10], "big") == UID
    assert int.from_bytes(scale.assigned[14:16], "big") == 1300
    assert scale.assigned[18] == 7
    # reference weight written so the scale recognises us next time
    assert scale.set_weight is not None and scale.reference


async def test_unknown_with_full_body_composition_from_live_frame():
    live = frames_for_record(0xE7, 0x4C, record(70.0))
    scale = FakeScale(users=[(UID, "BOB", 1990)], live=live, reference=True,
                      unknown=[])
    # Weigh-in arrives live as unowned; the scale lists it once we ask again.
    orig = scale.write_gatt_char

    async def write(char, data, response):
        if data[1:2] == b"\x40":
            scale.unknown.append((3, NOW - 5, 1400, 480))
        await orig(char, data, response)

    scale.write_gatt_char = write
    result = await session(scale).run()
    assert best(result).weight_kg == 70.0
    assert best(result).body_fat == 21.5  # full composition kept
    assert scale.assigned is not None


async def test_unknown_not_assigned_when_it_could_be_someone_else():
    scale = FakeScale(
        users=[(UID, "BOB", 1990), (0x99, "ANN", 1985)],
        unknown=[(7, NOW - 30, 1300, 500)],
        reference=True,
    )
    result = await session(scale).run()
    assert scale.assigned is None
    assert best(result) is None


async def test_stale_unknown_not_assigned():
    scale = FakeScale(users=[(UID, "BOB", 1990)],
                      unknown=[(7, NOW - 3 * 3600, 1300, 500)], reference=True)
    result = await session(scale).run()
    assert scale.assigned is None
    assert best(result) is None


async def test_new_user_without_reference_adopts_even_with_other_users():
    scale = FakeScale(
        users=[(UID, "BOB", 1990), (0x99, "ANN", 1985)],
        unknown=[(7, NOW - 30, 1300, 500)],
    )
    result = await session(scale).run()
    assert scale.assigned is not None
    assert best(result).weight_kg == 65.0


async def test_two_people_drained_in_one_session():
    ann_rec = record(58.0)
    scale = FakeScale(users=[(UID, "BOB", 1990), (0x99, "ANN", 1985)])
    orig = scale.write_gatt_char
    store = {UID: [record(80.0)], 0x99: [ann_rec]}

    async def write(char, data, response):
        if data[1:2] == b"\x41":  # serve per-user saved lists
            uid = int.from_bytes(data[2:10], "big")
            scale.saved = list(store[uid])
        await orig(char, data, response)

    scale.write_gatt_char = write
    result = await session(scale, people=[PERSON, ANN]).run()
    assert best(result, "bob").weight_kg == 80.0
    assert best(result, "ann").weight_kg == 58.0
    assert result.battery == 94


async def test_live_record_routed_to_the_right_person():
    live = frames_for_record(0xE7, 0x59, record(58.5), 0x99)
    scale = FakeScale(users=[(UID, "BOB", 1990), (0x99, "ANN", 1985)], live=live)
    scale.push_live = True
    result = await session(scale, people=[PERSON, ANN]).run()
    assert best(result, "ann").weight_kg == 58.5
    assert best(result, "bob") is None
    # with several people we listen instead of forcing a measurement for one
    assert not any(w[1:2] == b"\x40" for w in scale.written)


async def test_multi_person_picks_up_record_stored_during_wait():
    """Scale stored the weigh-in itself instead of pushing it live."""
    scale = FakeScale(users=[(UID, "BOB", 1990), (0x99, "ANN", 1985)])
    orig = scale.write_gatt_char
    calls = {"n": 0}

    async def write(char, data, response):
        if data[1:2] == b"\x41" and int.from_bytes(data[2:10], "big") == 0x99:
            calls["n"] += 1
            scale.saved = [record(59.0)] if calls["n"] >= 2 else []
        elif data[1:2] == b"\x41":
            scale.saved = []
        await orig(char, data, response)

    scale.write_gatt_char = write
    result = await session(scale, people=[PERSON, ANN]).run()
    assert best(result, "ann").weight_kg == 59.0
