"""One connected session with a Beurer/Sanitas scale, for any number of people.

Independent of Home Assistant: it only needs an object with ``start_notify``,
``stop_notify`` and ``write_gatt_char`` (a ``bleak`` client).

One session per wake-up: sync clock, make sure every configured person has a
slot on the scale, drain their stored weigh-ins, and (if nothing was stored)
wait for a live weigh-in.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
from typing import Any, Protocol

from .protocol import (
    CHAR_UUID,
    CMD_ASSIGN_UNKNOWN,
    CMD_DELETE_SAVED,
    CMD_DO_MEASUREMENT,
    CMD_GET_SAVED,
    CMD_GET_UNKNOWN,
    CMD_GET_USER_WEIGHT,
    CMD_LIVE_WEIGHT,
    CMD_MEASUREMENT,
    CMD_SAVED_MEASUREMENT,
    CMD_SCALE_STATUS,
    CMD_SET_UNIT,
    CMD_SET_USER_WEIGHT,
    CMD_UNKNOWN_INFO,
    CMD_UNKNOWN_LIVE,
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
    UnknownMeasurement,
    UserProfile,
    alt_start,
    build_app_ack,
    build_assign_unknown,
    build_command,
    build_init,
    build_set_time,
    build_set_user_weight,
    decode_measurement,
    details_match_profile,
    encode_uid,
    has_reference_weight,
    is_scale_ack,
    parse_scale_status,
    parse_unknown_info,
    parse_user_details,
    parse_user_info,
    remote_matches_profile,
    start_byte_candidates,
    weight_only_measurement,
)

_LOGGER = logging.getLogger(__name__)

# Only adopt an unowned weigh-in that happened this recently.
UNKNOWN_MAX_AGE = 15 * 60


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


@dataclass(frozen=True, slots=True)
class Person:
    """A person we want measurements for."""

    key: str  # stable id, e.g. the subentry id
    uid: int
    profile: UserProfile


@dataclass(slots=True)
class SessionResult:
    """What a session produced."""

    battery: int | None = None
    # person key -> new uid, for people adopted from slots created by another app
    adopted: dict[str, int] = field(default_factory=dict)
    # person key -> valid weigh-ins, oldest first
    measurements: dict[str, list[Measurement]] = field(default_factory=dict)
    discarded: list[str] = field(default_factory=list)


class ScaleSession:
    """Run the connect -> sync -> drain -> (measure) conversation."""

    def __init__(
        self,
        client: GattClient,
        name: str | None,
        people: Sequence[Person],
        *,
        measure_timeout: float = 45.0,
        step_timeout: float = 5.0,
    ) -> None:
        """Initialise the session."""
        self._client = client
        self._candidates = start_byte_candidates(name)
        self._people = list(people)
        self._uids: dict[str, int] = {p.key: p.uid for p in people}
        self._measure_timeout = measure_timeout
        self._step_timeout = step_timeout

        self._start = self._candidates[0]
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._assembler = RecordAssembler()
        self._users: dict[int, RemoteUser] = {}
        self._saved: dict[int, list[Measurement]] = defaultdict(list)
        self._saved_target: int | None = None
        self._live: dict[int, list[Measurement]] = defaultdict(list)
        self._discarded: list[str] = []
        self._unknown: dict[int, UnknownMeasurement] = {}
        self._unknown_live: bytes | None = None
        self._result = SessionResult()

    # -- plumbing ----------------------------------------------------------

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        frame = bytes(data)
        _LOGGER.debug("<- %s", frame.hex(" "))
        if frame and frame[0] in (self._start, alt_start(self._start, NIBBLE_INIT)):
            self._queue.put_nowait(frame)

    async def _write(self, data: bytes) -> None:
        _LOGGER.debug("-> %s", data.hex(" "))
        await self._client.write_gatt_char(CHAR_UUID, data, True)

    def _person_for_uid(self, uid: int) -> Person | None:
        for person in self._people:
            if self._uids[person.key] == uid:
                return person
        return None

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
            if (
                buf := self._assembler.feed_saved(frame)
            ) is not None and self._saved_target is not None:
                self._store(self._saved[self._saved_target], buf, "saved")
        elif cmd == CMD_MEASUREMENT:
            await self._write(build_app_ack(self._start, frame))
            uid, buf = self._assembler.feed_live(frame)
            if buf is not None and uid is not None:
                if self._person_for_uid(uid) is not None:
                    self._store(self._live[uid], buf, "live")
                else:
                    _LOGGER.debug("Ignoring measurement of unconfigured user %s", uid)
        elif cmd == CMD_UNKNOWN_INFO:
            await self._write(build_app_ack(self._start, frame))
            if (unknown := parse_unknown_info(frame)) is not None:
                self._unknown[unknown.ident] = unknown
        elif cmd == CMD_UNKNOWN_LIVE:
            await self._write(build_app_ack(self._start, frame))
            if (buf := self._assembler.feed_unknown(frame)) is not None:
                self._unknown_live = buf
        elif cmd == CMD_LIVE_WEIGHT:
            _LOGGER.debug("Live weight frame (ignored; only final records count)")

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

    def _any_live(self) -> bool:
        return any(self._live.values())

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
        if (ack[4] if len(ack) > 4 else 0) == 0:
            return
        await self._expect(
            lambda f: f[1] == CMD_USER_INFO and len(f) > 3 and f[3] >= f[2]
        )

    async def _resolve_user(self, person: Person) -> None:
        """Make sure the person has a slot on the scale."""
        uid = self._uids[person.key]
        taken = {self._uids[p.key] for p in self._people if p.key != person.key}
        if any(u.uid == uid for u in self._users.values()):
            return
        for user in self._users.values():
            if user.uid not in taken and remote_matches_profile(user, person.profile):
                _LOGGER.info("Adopting existing scale user %s (%s)", user.name, user.uid)
                self._uids[person.key] = user.uid
                self._result.adopted[person.key] = user.uid
                return
        ack = await self._request(CMD_USER_ADD, person.profile.payload(uid))
        if len(ack) < 4 or ack[3] != 0:
            raise SessionError("scale refused to add user (user list full?)")
        _LOGGER.debug("Added %s to the scale", person.profile.nickname)

    async def _sync_profile(self, person: Person) -> None:
        uid = self._uids[person.key]
        ack = await self._request(CMD_USER_DETAILS, encode_uid(uid))
        details = parse_user_details(ack)
        if details is None or details_match_profile(details, person.profile):
            return
        _LOGGER.info(
            "Updating profile stored on the scale for %s", person.profile.nickname
        )
        upd = await self._request(CMD_USER_UPDATE, person.profile.payload(uid))
        if len(upd) < 4 or upd[3] != 0:
            _LOGGER.warning("Scale rejected profile update (status %s)", upd[3:4].hex())

    async def _fetch_saved(self, person: Person) -> None:
        uid = self._uids[person.key]
        self._saved_target = uid
        ack = await self._request(CMD_GET_SAVED, encode_uid(uid))
        count = ack[4] if len(ack) > 4 and ack[3] == 0 else 0
        if not count:
            return
        with suppress(SessionError):
            await self._expect(
                lambda f: f[1] == CMD_SAVED_MEASUREMENT and len(f) > 3 and f[3] >= f[2],
                timeout=self._step_timeout * 2,
            )

    async def _delete_saved(self, uid: int) -> None:
        with suppress(SessionError):
            await self._request(CMD_DELETE_SAVED, encode_uid(uid))

    async def _take_unknown(self) -> None:
        """Adopt a recent weigh-in the scale could not attribute to anyone.

        This is how the official app teaches the scale who you are: fetch the
        unknown list, assign the newest entry to the user, then store the
        weight as the user's reference weight. We only do it when it is clear
        whose weigh-in it was: the scale has a single user, or exactly one of
        our people has no reference weight yet.
        """
        self._unknown.clear()
        ack = await self._request(CMD_GET_UNKNOWN)
        if len(ack) < 4 or ack[3] == 1:  # 1 == none stored
            return
        with suppress(SessionError):
            await self._expect(
                lambda f: f[1] == CMD_UNKNOWN_INFO and len(f) > 3 and f[3] >= f[2]
            )
        now = datetime.now(UTC).timestamp()
        recent = [
            u
            for u in sorted(self._unknown.values(), key=lambda u: u.epoch)
            if abs(now - u.epoch) <= UNKNOWN_MAX_AGE
        ]
        if not recent:
            return

        has_ref: dict[str, bool] = {}
        for person in self._people:
            reply = await self._request(
                CMD_GET_USER_WEIGHT, encode_uid(self._uids[person.key])
            )
            has_ref[person.key] = has_reference_weight(reply)

        target: Person | None = None
        if len(self._users) == 1 and len(self._people) == 1:
            target = self._people[0]
        else:
            without = [p for p in self._people if not has_ref[p.key]]
            if len(without) == 1:
                target = without[0]
        if target is None:
            _LOGGER.debug("Leaving unowned weigh-in alone: could be someone else")
            return

        uid = self._uids[target.key]
        unknown = recent[-1]
        await self._write(build_assign_unknown(self._start, uid, unknown))
        ack = await self._expect(
            lambda f: is_scale_ack(f, self._start, CMD_ASSIGN_UNKNOWN)
        )
        if len(ack) < 4 or ack[3] != 0:
            _LOGGER.warning("Scale rejected assignment (status %s)", ack[3:4].hex())
            return
        _LOGGER.debug("Assigned unowned weigh-in to %s", target.profile.nickname)
        with suppress(SessionError):  # the scale may follow up with a full record
            await self._expect(lambda _f: bool(self._live[uid]), 3.0)
        if not self._live[uid]:
            measurement = (
                decode_measurement(self._unknown_live) if self._unknown_live else None
            )
            if measurement is None or not measurement.is_valid:
                measurement = weight_only_measurement(unknown)
            if measurement.is_valid:
                self._live[uid].append(measurement)
        measurement = self._live[uid][-1] if self._live[uid] else None
        if not has_ref[target.key] and measurement is not None:
            fat = round((measurement.body_fat or 0) * 10)
            with suppress(SessionError):
                await self._write(
                    build_set_user_weight(
                        self._start,
                        uid,
                        measurement.raw_weight,
                        fat,
                        unknown.timestamp_raw,
                    )
                )
                await self._expect(
                    lambda f: is_scale_ack(f, self._start, CMD_SET_USER_WEIGHT)
                )

    async def _measure(self) -> None:
        """Wait for a live weigh-in.

        With a single person we ask the scale to measure for them (this also
        teaches it who they are). With several we just listen: the scale picks
        the person by weight.
        """
        if len(self._people) == 1:
            uid = self._uids[self._people[0].key]
            ack = await self._request(CMD_DO_MEASUREMENT, encode_uid(uid))
            if len(ack) < 4 or ack[3] != 0:
                raise SessionError("scale refused to start a measurement")
        with suppress(SessionError):
            await self._expect(
                lambda _f: self._any_live() or self._unknown_live is not None,
                self._measure_timeout,
            )
        if not self._any_live() and self._unknown_live is not None:
            await self._take_unknown()

    # -- entry point -------------------------------------------------------

    async def run(self) -> SessionResult:
        """Run the session."""
        await self._client.start_notify(CHAR_UUID, self._on_notify)
        try:
            await self._init()
            await self._write(build_set_time(self._start, datetime.now(UTC)))
            status = await self._status()
            self._result.battery = status.battery
            await self._list_users()
            for person in self._people:
                await self._resolve_user(person)
                await self._sync_profile(person)
            for person in self._people:
                await self._fetch_saved(person)
            self._saved_target = None
            try:
                if not any(self._saved.values()):
                    await self._take_unknown()
                if not any(self._saved.values()) and not self._any_live():
                    await self._measure()
            except SessionError as err:
                _LOGGER.debug("Live phase ended early: %s", err)
            if not any(self._saved.values()) and not self._any_live():
                # The scale may have stored a weigh-in while we were waiting
                # (it only pushes live results for a user we asked about).
                for person in self._people:
                    with suppress(SessionError):
                        await self._fetch_saved(person)
                self._saved_target = None

            for person in self._people:
                uid = self._uids[person.key]
                found = sorted(
                    [*self._saved[uid], *self._live[uid]], key=lambda m: m.timestamp
                )
                if found:
                    self._result.measurements[person.key] = found
                    await self._delete_saved(uid)
            self._result.discarded = self._discarded
            return self._result
        finally:
            with suppress(Exception):
                await self._client.stop_notify(CHAR_UUID)
