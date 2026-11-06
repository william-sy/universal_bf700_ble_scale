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
    ScaleSession,
    SessionError,
)

UID = 0x1234
PROFILE = UserProfile("Bob", 180, date(1990, 5, 17), True, 3)
NOW = int(datetime.now(UTC).timestamp())


def record(weight_kg: float, *, imp=500, fat=215, water=550, muscle=420, bone=60,
           ts=NOW) -> bytes:
    return struct.pack(">IHHHHHH", ts, round(weight_kg / 0.05), imp, fat, water, muscle, bone)


def frames_for_record(start: int, cmd: int, rec: bytes, uid: int | None = None):
    """Split a 16-byte record into two 20-byte frames like the scale does."""
    pad = rec + bytes(16)
    if cmd == 0x42:
        return [
            bytes([start, 0x42, 2, 1]) + pad[:16],
            bytes([start, 0x42, 2, 2]) + pad[16:32],
        ]
    return [
        bytes([start, 0x59, 3, 1, 0]) + uid.to_bytes(8, "big") + bytes(6),
        bytes([start, 0x59, 3, 2]) + pad[:16],
        bytes([start, 0x59, 3, 3]) + pad[16:32],
    ]


class FakeScale:
    """Minimal scale simulator speaking the E7 or F7 dialect."""

    def __init__(self, start=0xE7, users=(), saved=(), live=None, speaks=None):
        self.start = start
        self.speaks = speaks or start
        self.users = list(users)  # (uid, name, year)
        self.saved = list(saved)
        self.live = live  # list of frames sent after DO_MEASUREMENT
        self.written: list[bytes] = []
        self.cb = None

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
        elif cmd == 0x40:
            self._send(bytes([s, 0xF0, 0x40, 0]))
            if self.live:
                self._send(*self.live)
        # app acks (0xF1) need no reply


def session(scale: FakeScale, name="SANITAS SBF70", uid=UID, **kw):
    return ScaleSession(scale, name, PROFILE, uid, measure_timeout=1.0, step_timeout=1.0, **kw)


async def test_live_measurement_existing_user():
    live = [
        bytes([0xE7, 0x58, 1, 0, 0]),  # settling: the 12.80 trap
        bytes([0xE7, 0x58, 0, 0x05, 0x14]),
        *frames_for_record(0xE7, 0x59, record(82.4), UID),
    ]
    scale = FakeScale(users=[(UID, "BOB", 1990)], live=live)
    result = await session(scale).run()
    assert result.measurement.weight_kg == 82.4
    assert result.measurement.body_fat == 21.5
    assert result.measurement.body_water == 55.0
    assert result.measurement.muscle == 42.0
    assert result.measurement.bone_kg == 3.0
    assert result.battery == 94
    assert result.live_weight_kg == 65.0  # 0x0514 * 50 g; unstable frame ignored
    assert not result.uid_adopted
    assert not any(w[1] == 0x31 for w in scale.written if len(w) > 1)  # no user add


async def test_saved_measurement_then_deleted():
    scale = FakeScale(users=[(UID, "BOB", 1990)], saved=[record(70.0)])
    result = await session(scale).run()
    assert result.measurement.weight_kg == 70.0
    assert scale.saved == []
    assert not any(w[1:2] == b"\x40" for w in scale.written)  # no live measure needed


async def test_adds_unknown_user():
    scale = FakeScale(live=frames_for_record(0xE7, 0x59, record(60.0), UID))
    result = await session(scale).run()
    assert scale.users and scale.users[0][0] == UID
    assert result.measurement.weight_kg == 60.0


async def test_adopts_user_created_by_other_app():
    scale = FakeScale(users=[(0xABCD, "BOB", 1990)],
                      live=frames_for_record(0xE7, 0x59, record(61.0), 0xABCD))
    result = await session(scale).run()
    assert result.uid == 0xABCD and result.uid_adopted
    assert result.measurement.weight_kg == 61.0


async def test_other_users_measurement_ignored():
    scale = FakeScale(users=[(UID, "BOB", 1990)],
                      live=frames_for_record(0xE7, 0x59, record(95.0), 0x9999))
    result = await session(scale).run()
    assert result.measurement is None


async def test_f7_dialect_autodetected_despite_name():
    scale = FakeScale(start=0xF7, users=[(UID, "BOB", 1990)],
                      live=frames_for_record(0xF7, 0x59, record(75.0), UID))
    # Name hints E7 first; session must fall back to F7.
    result = await session(scale, name="Some Scale").run()
    assert result.measurement.weight_kg == 75.0


async def test_stuck_12_80_placeholder_is_discarded():
    rec = record(12.8, imp=0, fat=0, water=0, muscle=0, bone=0)
    scale = FakeScale(users=[(UID, "BOB", 1990)],
                      live=frames_for_record(0xE7, 0x59, rec, UID))
    result = await session(scale).run()
    assert result.measurement is None
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
