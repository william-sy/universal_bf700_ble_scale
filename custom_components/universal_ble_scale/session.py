"""One connected session with a Beurer/Sanitas scale.

Independent of Home Assistant: it only needs an object with ``start_notify``,
``stop_notify`` and ``write_gatt_char`` (a ``bleak`` client).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
from typing import Any, Protocol

from .protocol import (
    CHAR_UUID,
    CMD_DELETE_SAVED,
    CMD_DO_MEASUREMENT,
    CMD_GET_SAVED,
    CMD_LIVE_WEIGHT,
    CMD_MEASUREMENT,
    CMD_SAVED_MEASUREMENT,
    CMD_SCALE_STATUS,
    CMD_SET_UNIT,
    CMD_USER_ADD,
    CMD_USER_DETAILS,
    CMD_USER_INFO,
    CMD_USER_LIST,
    CMD_USER_UPDATE,
    NIBBLE_INIT,
    UNIT_KG,
    Measurement,
    RecordAssembler,
    RemoteUser,
    ScaleStatus,
    UserProfile,
    alt_start,
    build_app_ack,
    build_command,
    build_init,
    build_set_time,
    decode_measurement,
    details_match_profile,
    encode_uid,
    is_scale_ack,
    parse_live_weight,
    parse_scale_status,
    parse_user_details,
    parse_user_info,
    remote_matches_profile,
    start_byte_candidates,
)

_LOGGER = logging.getLogger(__name__)


class GattClient(Protocol):
    """The subset of ``BleakClient`` that we use."""

    async def start_notify(self, char: str, callback: Callable[..., Any]) -> None:
        """Subscribe to notifications."""

    async def stop_notify(self, char: str) -> None:
        """Unsubscribe from notifications."""

    async def write_gatt_char(self, char: str, data: bytes, response: bool) -> None:
        """Write to a characteristic."""


class SessionError(Exception):
    """The scale refused or did not answer a request."""


@dataclass(slots=True)
class SessionResult:
    """What a session produced."""

    uid: int
    uid_adopted: bool = False
    battery: int | None = None
    measurement: Measurement | None = None
    live_weight_kg: float | None = None
    discarded: list[str] = field(default_factory=list)


class ScaleSession:
    """Run the connect -> sync -> measure conversation."""

    def __init__(
        self,
        client: GattClient,
        name: str | None,
        profile: UserProfile,
        uid: int,
        *,
        measure_timeout: float = 30.0,
        step_timeout: float = 5.0,
        on_live_weight: Callable[[float], None] | None = None,
    ) -> None:
        """Initialise the session."""
        self._client = client
        self._candidates = start_byte_candidates(name)
        self._profile = profile
        self._uid = uid
        self._measure_timeout = measure_timeout
        self._step_timeout = step_timeout
        self._on_live_weight = on_live_weight

        self._start = self._candidates[0]
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._assembler = RecordAssembler()
        self._users: dict[int, RemoteUser] = {}
        self._saved: list[Measurement] = []
        self._live: list[Measurement] = []
        self._discarded: list[str] = []
        self._live_weight: float | None = None

    # -- plumbing ----------------------------------------------------------

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        frame = bytes(data)
        _LOGGER.debug("<- %s", frame.hex(" "))
        if frame and frame[0] in (self._start, alt_start(self._start, NIBBLE_INIT)):
            self._queue.put_nowait(frame)

    async def _write(self, data: bytes) -> None:
        _LOGGER.debug("-> %s", data.hex(" "))
        await self._client.write_gatt_char(CHAR_UUID, data, True)

    async def _process(self, frame: bytes) -> None:
        """Side effects for frames that need an ack or carry data."""
        if len(frame) < 2 or frame[0] != self._start:
            return
        cmd = frame[1]
        if cmd == CMD_USER_INFO:
            await self._write(build_app_ack(self._start, frame))
            if (user := parse_user_info(frame)) is not None:
                self._users[user.index] = user
        elif cmd == CMD_SAVED_MEASUREMENT:
            await self._write(build_app_ack(self._start, frame))
            if (buf := self._assembler.feed_saved(frame)) is not None:
                self._store(self._saved, buf, "saved")
        elif cmd == CMD_MEASUREMENT:
            await self._write(build_app_ack(self._start, frame))
            uid, buf = self._assembler.feed_live(frame)
            if buf is not None:
                if uid == self._uid:
                    self._store(self._live, buf, "live")
                else:
                    _LOGGER.debug("Ignoring measurement of other user %s", uid)
        elif cmd == CMD_LIVE_WEIGHT:
            live = parse_live_weight(frame)
            if live is not None and live.trustworthy:
                self._live_weight = live.weight_kg
                if self._on_live_weight:
                    self._on_live_weight(live.weight_kg)

    def _store(self, target: list[Measurement], buf: bytes, kind: str) -> None:
        measurement = decode_measurement(buf, now=datetime.now(UTC))
        if measurement is None:
            self._discarded.append(f"{kind}: short record {buf.hex()}")
        elif not measurement.is_valid:
            # Includes the notorious 12.80 kg placeholder.
            _LOGGER.warning(
                "Discarding implausible %s record (%.2f kg, raw 0x%04x): %s",
                kind,
                measurement.weight_kg,
                measurement.raw_weight,
                buf.hex(),
            )
            self._discarded.append(f"{kind}: {measurement.weight_kg} kg")
        else:
            target.append(measurement)

    async def _expect(
        self, predicate: Callable[[bytes], bool], timeout: float | None = None
    ) -> bytes:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (timeout or self._step_timeout)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise SessionError("timed out waiting for scale")
            try:
                frame = await asyncio.wait_for(self._queue.get(), remaining)
            except TimeoutError as err:
                raise SessionError("timed out waiting for scale") from err
            await self._process(frame)
            if predicate(frame):
                return frame

    async def _request(self, cmd: int, payload: bytes = b"") -> bytes:
        """Send a command and wait for its 0xF0 ack."""
        await self._write(build_command(self._start, cmd, payload))
        return await self._expect(lambda f: is_scale_ack(f, self._start, cmd))

    # -- steps -------------------------------------------------------------

    async def _init(self) -> None:
        for start in self._candidates:
            self._start = start
            await self._write(build_init(start))
            try:
                await self._expect(
                    lambda f: f[0] == alt_start(start, NIBBLE_INIT), timeout=4.0
                )
            except SessionError:
                _LOGGER.debug("No init ack with start byte 0x%02x", start)
                continue
            _LOGGER.debug("Scale speaks start byte 0x%02x", start)
            return
        raise SessionError("scale did not answer init (wrong device or asleep)")

    async def _status(self) -> ScaleStatus:
        frame = await self._request(CMD_SCALE_STATUS, encode_uid(None))
        status = parse_scale_status(frame)
        if status is None:
            raise SessionError("unparseable status")
        if status.unit != UNIT_KG:
            await self._write(build_command(self._start, CMD_SET_UNIT, bytes([UNIT_KG])))
        return status

    async def _list_users(self) -> None:
        self._users.clear()
        await self._write(build_command(self._start, CMD_USER_LIST))
        # Reply: f0 33 <st> <count> <max>, then <count> x 0x34 frames.
        ack = await self._expect(lambda f: is_scale_ack(f, self._start, CMD_USER_LIST))
        count = ack[4] if len(ack) > 4 else 0
        if count == 0:
            return
        await self._expect(
            lambda f: f[1] == CMD_USER_INFO and len(f) > 3 and f[3] >= f[2]
        )

    async def _resolve_user(self) -> bool:
        """Make sure our uid exists on the scale. Return True if adopted."""
        if any(u.uid == self._uid for u in self._users.values()):
            return False
        for user in self._users.values():
            if remote_matches_profile(user, self._profile):
                _LOGGER.info("Adopting existing scale user %s (%s)", user.name, user.uid)
                self._uid = user.uid
                return True
        ack = await self._request(CMD_USER_ADD, self._profile.payload(self._uid))
        if len(ack) < 4 or ack[3] != 0:
            raise SessionError("scale refused to add user (user list full?)")
        return False

    async def _sync_profile(self) -> None:
        ack = await self._request(CMD_USER_DETAILS, encode_uid(self._uid))
        details = parse_user_details(ack)
        if details is None or details_match_profile(details, self._profile):
            return
        _LOGGER.info("Updating profile stored on the scale")
        upd = await self._request(CMD_USER_UPDATE, self._profile.payload(self._uid))
        if len(upd) < 4 or upd[3] != 0:
            _LOGGER.warning("Scale rejected profile update (status %s)", upd[3:4].hex())

    async def _fetch_saved(self) -> None:
        ack = await self._request(CMD_GET_SAVED, encode_uid(self._uid))
        count = ack[4] if len(ack) > 4 and ack[3] == 0 else 0
        if not count:
            return
        with suppress(SessionError):
            await self._expect(
                lambda f: f[1] == CMD_SAVED_MEASUREMENT
                and len(f) > 3
                and f[3] >= f[2],
                timeout=self._step_timeout * 2,
            )

    async def _delete_saved(self) -> None:
        with suppress(SessionError):
            await self._request(CMD_DELETE_SAVED, encode_uid(self._uid))

    async def _measure(self) -> None:
        ack = await self._request(CMD_DO_MEASUREMENT, encode_uid(self._uid))
        if len(ack) < 4 or ack[3] != 0:
            raise SessionError("scale refused to start a measurement")
        with suppress(SessionError):
            await self._expect(lambda _f: bool(self._live), self._measure_timeout)

    # -- entry point -------------------------------------------------------

    async def run(self) -> SessionResult:
        """Run the session."""
        await self._client.start_notify(CHAR_UUID, self._on_notify)
        try:
            await self._init()
            await self._write(build_set_time(self._start, datetime.now(UTC)))
            status = await self._status()
            await self._list_users()
            adopted = await self._resolve_user()
            await self._sync_profile()
            await self._fetch_saved()
            if not self._saved:
                await self._measure()
            candidates = [*self._saved, *self._live]
            best = max(candidates, key=lambda m: m.timestamp, default=None)
            if candidates:
                await self._delete_saved()
            return SessionResult(
                uid=self._uid,
                uid_adopted=adopted,
                battery=status.battery,
                measurement=best,
                live_weight_kg=self._live_weight,
                discarded=self._discarded,
            )
        finally:
            with suppress(Exception):
                await self._client.stop_notify(CHAR_UUID)

