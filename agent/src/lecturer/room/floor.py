"""Who may speak, and who is speaking. Plain code, beside the voice decider.

Students are muted by default. There are three ways the microphones work:

    closed   nobody but staff can speak
    hands    a student raises a hand and is given the floor, one at a time
    open     any student may unmute (the lecturer wants answers, or a discussion)

Staff (a human helper, or a guest lecturer) may speak at any time and may share
their screen. Whenever a person is speaking, the lecturer stops and waits.

A student's microphone is a permission that LiveKit enforces. It is granted here
and taken back here; a browser cannot grant it to itself. A person's role comes
from their join token, which our server signs, so it cannot be faked either.
Every student is muted when the lecturer arrives and again when it leaves, so a
restart or a crash never leaves a microphone open with nobody minding it.

What is said is written down by `speech/hearing.py`, if a model for it is set,
and the live teacher is told. When a turn ends, the teacher is told whether anything
was heard (`on_turn`), so that a student whose microphone did not work is not passed
over.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from livekit import api, rtc

from ..speech.holds import Hold

if TYPE_CHECKING:
    from ..speech.decider import VoiceDecider
    from .events import ClassEvents

logger = logging.getLogger("lecturer.floor")

ROLE_ATTR = "ch.role"
STAFF = ("helper", "guest")
MODES = ("closed", "hands", "open")

QUIET_SECONDS = 1.5  # the lecturer carries on this long after the last person stops
FLOOR_NEVER_SPOKE_SECONDS = 20  # given the floor but said nothing
FLOOR_SILENCE_SECONDS = 6  # the follow-up window after they stop
FLOOR_MAX_SECONDS = 120
MAX_HANDS = 30
HAND_AGAIN_SECONDS = 20  # after a turn, before the same student can raise a hand again


@dataclass
class FloorState:
    """Live state for the snapshot. The browser has the same rules in store.ts."""

    mode: str = "hands"
    prompt: str = ""  # shown with open mics, e.g. what the lecturer asked
    hands: list[dict[str, str]] = field(default_factory=list)  # raised, in order
    speaker: dict[str, str] | None = None  # the student who has the floor
    paused: bool = False  # a helper has paused the lecturer

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "prompt": self.prompt,
            "hands": [dict(h) for h in self.hands],
            "speaker": dict(self.speaker) if self.speaker else None,
            "paused": self.paused,
        }

    @classmethod
    def from_snapshot(cls, snap: dict[str, Any]) -> FloorState:
        return cls(
            snap["mode"],
            snap["prompt"],
            [dict(h) for h in snap["hands"]],
            snap["speaker"],
            snap.get("paused", False),
        )

    def apply(self, ev: dict[str, Any]) -> None:
        t = ev["type"]
        if t == "floor.mode":
            self.mode, self.prompt = ev["mode"], ev.get("prompt", "")
            if self.mode != "hands":
                self.hands = []
        elif t == "floor.hand":
            self.hands = [h for h in self.hands if h["identity"] != ev["identity"]]
            if ev["up"]:
                self.hands.append({"identity": ev["identity"], "name": ev["name"]})
        elif t == "floor.give":
            self.hands = [h for h in self.hands if h["identity"] != ev["identity"]]
            self.speaker = {"identity": ev["identity"], "name": ev["name"]}
        elif t == "floor.take":
            self.speaker = None
        elif t == "floor.pause":
            self.paused = bool(ev["on"])


PERSON_ATTR = "ch.person"  # who this is: the same on every tab of one person


def person_of(participant: rtc.Participant | None, identity: str = "") -> str:
    """Who this is, counting someone in two tabs (or windows) once. The join server
    puts the same `ch.person` on every tab of one person; with real accounts, it would
    be the account."""
    if participant is None:
        return identity
    return participant.attributes.get(PERSON_ATTR, "") or participant.identity


def role_of(participant: rtc.Participant | None) -> str:
    """student, helper or guest. Read from the token-set attribute."""
    role = participant.attributes.get(ROLE_ATTR, "") if participant else ""
    return role if role in STAFF else "student"


def spoken_name(name: str) -> str:
    """A name made safe to say aloud and to send to a voice service.

    A student chooses their own name, and the lecturer says it ("Ada, go ahead").
    Only letters, digits, spaces, hyphens and apostrophes are kept, and not many of
    them, so a name cannot be a sentence or a command to the voice.
    """
    # Letters with their accents and marks, digits, spaces, hyphens and apostrophes.
    name = unicodedata.normalize("NFC", name)
    kept = "".join(c if unicodedata.category(c)[0] in "LMN" or c in " '-" else " " for c in name)
    return re.sub(r"\s+", " ", kept).strip()[:24].strip()


class Floor:
    def __init__(self, room: rtc.Room, events: ClassEvents, decider: VoiceDecider) -> None:
        self._room = room
        self._events = events
        self._decider = decider
        self._lk: api.LiveKitAPI | None = None
        # Mode changes, giving the floor and taking it back happen one at a time, so
        # two of them cannot leave a microphone in the wrong state between them.
        self._lock = asyncio.Lock()
        self._changed = asyncio.Event()
        self._done = asyncio.Event()  # the speaker pressed done, or staff took the floor
        self._free = asyncio.Event()  # nobody has the floor
        self._free.set()
        self._last_spoke: dict[str, float] = {}
        # Told when a student's turn with the floor ends: (identity, whether they spoke).
        self.on_turn: Callable[[str, bool], None] | None = None
        self._had_floor: dict[str, float] = {}
        self._hand_calls: dict[str, list[float]] = {}
        self._quiet_timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stage_before_share: list[list[dict[str, Any]]] | None = None
        self._sharing = ""  # identity of the person whose screen is on the stage

    @property
    def state(self) -> FloorState:
        return self._events.floor

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def register(self) -> None:
        local = self._room.local_participant
        local.register_rpc_method("ch.hand", self._on_hand)
        local.register_rpc_method("ch.done", self._on_done)
        local.register_rpc_method("ch.staff", self._on_staff)
        self._room.on("active_speakers_changed", self._on_speakers)
        self._room.on("track_published", self._on_track_published)
        self._room.on("track_unpublished", self._on_track_unpublished)
        self._room.on("participant_connected", self._on_joined)
        self._room.on("participant_disconnected", self._on_left)
        # Whatever an earlier lecturer left behind, start with every student muted.
        self._spawn(self.mute_everyone())
        # People who were here before us.
        for person in self._room.remote_participants.values():
            for publication in person.track_publications.values():
                self._on_track_published(publication, person)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._quiet_timer:
            self._quiet_timer.cancel()
        # Leave nobody with an open microphone and no lecturer to mind it.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.mute_everyone(), timeout=5)
        if self._lk:
            await self._lk.aclose()
            self._lk = None

    async def wait_free(self) -> None:
        """Wait until no student has the floor."""
        await self._free.wait()

    # -- microphones ----------------------------------------------------------------

    async def _allow_mic(self, identity: str, allow: bool) -> bool:
        """Grant or take back a student's microphone. LiveKit enforces it.

        Returns False if LiveKit could not be told, after one more try.
        """
        if self._lk is None:
            url = os.environ["LIVEKIT_URL"].replace("ws", "http", 1)
            self._lk = api.LiveKitAPI(url)
        permission = api.ParticipantPermission(
            can_subscribe=True,
            can_publish_data=True,
            can_publish=allow,
            can_publish_sources=[api.TrackSource.MICROPHONE] if allow else [],
        )
        request = api.UpdateParticipantRequest(
            room=self._room.name, identity=identity, permission=permission
        )
        for attempt in (1, 2):
            try:
                await self._lk.room.update_participant(request)
                return True
            except Exception as e:
                if identity not in self._room.remote_participants:
                    return True  # they have left: nothing to change
                logger.warning("microphone permission not changed (try %d): %s", attempt, e)
                await asyncio.sleep(0.3)
        self._events.note("mic_permission_failed", allow=allow)
        return False

    def _students(self) -> list[rtc.RemoteParticipant]:
        agent = rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        people = self._room.remote_participants.values()
        return [p for p in people if p.kind != agent and role_of(p) == "student"]

    async def mute_everyone(self) -> None:
        await asyncio.gather(*(self._allow_mic(p.identity, False) for p in self._students()))

    async def set_mode(self, mode: str, prompt: str = "") -> None:
        if mode not in MODES:
            return
        async with self._lock:
            was = self.state.mode
            await self._events.emit({"type": "floor.mode", "mode": mode, "prompt": prompt})
            if mode == "open":
                await asyncio.gather(*(self._allow_mic(p.identity, True) for p in self._students()))
            elif was == "open":
                keep = self.state.speaker["identity"] if self.state.speaker else ""
                students = [p for p in self._students() if p.identity != keep]
                await asyncio.gather(*(self._allow_mic(p.identity, False) for p in students))
        self._changed.set()

    # -- the hand queue and the floor ------------------------------------------------

    async def run(self) -> None:
        """Give the floor to raised hands, one at a time."""
        while True:
            await self._changed.wait()
            self._changed.clear()
            while self.state.mode == "hands" and self.state.hands and not self.state.speaker:
                who = self.state.hands[0]
                # Finish the sentence, then invite them.
                name = spoken_name(who["name"])
                line = f"{name}, go ahead." if name else "Go ahead."
                await self._decider.interject(line, then_hold=Hold.FLOOR, source="invite")
                still_wants = any(h["identity"] == who["identity"] for h in self.state.hands)
                here = who["identity"] in self._room.remote_participants
                given = still_wants and here and self.state.mode == "hands"
                if given:
                    given = await self._give(who)
                if not given:
                    self._decider.resume(Hold.FLOOR)
                    if still_wants:
                        await self._hand(who["identity"], who["name"], up=False)
                    continue
                try:
                    await self._until_floor_ends(who["identity"])
                finally:
                    await asyncio.shield(self._take(who["identity"]))
                    if self.on_turn is not None:
                        self.on_turn(who["identity"], who["identity"] in self._last_spoke)

    async def _hand(self, identity: str, name: str, *, up: bool) -> None:
        await self._events.emit(
            {"type": "floor.hand", "identity": identity, "name": name, "up": up}
        )
        self._changed.set()

    async def _give(self, who: dict[str, str]) -> bool:
        async with self._lock:
            self._done.clear()
            self._last_spoke.pop(who["identity"], None)
            self._decider.hold(Hold.FLOOR)
            if not await self._allow_mic(who["identity"], True):
                return False  # they would have the floor and no microphone
            self._free.clear()
            await self._events.emit({"type": "floor.give", **who})
            return True

    async def _until_floor_ends(self, identity: str) -> None:
        began = time.monotonic()
        while not self._done.is_set():
            now = time.monotonic()
            spoke = self._last_spoke.get(identity)
            if identity not in self._room.remote_participants:
                return
            if now - began > FLOOR_MAX_SECONDS:
                return
            if spoke is None and now - began > FLOOR_NEVER_SPOKE_SECONDS:
                return
            if spoke is not None and now - spoke > FLOOR_SILENCE_SECONDS:
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._done.wait(), timeout=0.25)

    async def _take(self, identity: str) -> None:
        async with self._lock:
            self._had_floor[person_of(self._room.remote_participants.get(identity), identity)] = (
                time.monotonic()
            )
            if self.state.mode != "open":
                await self._allow_mic(identity, False)
            await self._events.emit({"type": "floor.take", "identity": identity})
            self._free.set()
            self._decider.resume(Hold.FLOOR)

    # -- what people ask for ---------------------------------------------------------

    def _caller(self, data: rtc.RpcInvocationData) -> rtc.RemoteParticipant | None:
        return self._room.remote_participants.get(data.caller_identity)

    async def _on_hand(self, data: rtc.RpcInvocationData) -> str:
        """A student raises or lowers their hand."""
        person = self._caller(data)
        if person is None:
            return "gone"
        if role_of(person) != "student":
            return "staff can speak without raising a hand"
        now = time.monotonic()
        calls = [t for t in self._hand_calls.get(person.identity, []) if now - t < 10]
        if len(calls) >= 6:
            return "slow down"
        self._hand_calls[person.identity] = [*calls, now]
        up = data.payload == "up"
        # One hand for each person, from whichever of their tabs it was raised.
        who = person_of(person)
        people = self._room.remote_participants
        mine = next(
            (
                h
                for h in self.state.hands
                if person_of(people.get(h["identity"]), h["identity"]) == who
            ),
            None,
        )
        raised = mine is not None
        if up and not raised:
            if self.state.mode != "hands" or len(self.state.hands) >= MAX_HANDS:
                return "not now"
            if now - self._had_floor.get(who, -1e9) < HAND_AGAIN_SECONDS:
                return "you have just had a turn"
        if up and not raised:
            await self._hand(person.identity, person.name or "A student", up=True)
        elif not up and mine is not None:
            await self._hand(mine["identity"], mine["name"], up=False)
        return "ok"

    async def _on_done(self, data: rtc.RpcInvocationData) -> str:
        """The student with the floor says they have finished."""
        if self.state.speaker and self.state.speaker["identity"] == data.caller_identity:
            self._done.set()
        return "ok"

    async def _on_staff(self, data: rtc.RpcInvocationData) -> str:
        """Things only a helper or guest lecturer may ask for."""
        if role_of(self._caller(data)) not in STAFF:
            raise rtc.RpcError(403, "only staff can do this")
        try:
            ask = json.loads(data.payload)
            action = ask["action"]
        except (ValueError, KeyError, TypeError):
            raise rtc.RpcError(400, "bad request") from None
        if action == "mode":
            await self.set_mode(str(ask.get("mode")), str(ask.get("prompt", ""))[:200])
        elif action == "take":
            self._done.set()
        elif action in ("pause", "resume"):
            await self._pause(action == "pause")
        else:
            raise rtc.RpcError(400, "unknown action")
        return "ok"

    async def _pause(self, on: bool) -> None:
        """A helper pauses the lecturer, or lets it go on. The class is told."""
        if on == self.state.paused:
            return
        if on:
            self._decider.hold(Hold.HELPER)
        else:
            self._decider.resume(Hold.HELPER)
        await self._events.emit({"type": "floor.pause", "on": on})

    # -- who is speaking -------------------------------------------------------------

    def _on_speakers(self, speakers: list[rtc.Participant]) -> None:
        """A person is speaking: the lecturer stops. When nobody is, it carries on
        after a short quiet."""
        agent = rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        me = self._room.local_participant.identity
        people = [p for p in speakers if p.identity != me and p.kind != agent]
        now = time.monotonic()
        for person in people:
            self._last_spoke[person.identity] = now
        if self._quiet_timer:
            self._quiet_timer.cancel()
            self._quiet_timer = None
        if people:
            self._decider.hold(Hold.SPEAKING)
        elif self._decider.held_for(Hold.SPEAKING):
            loop = asyncio.get_running_loop()
            self._quiet_timer = loop.call_later(QUIET_SECONDS, self._quiet)

    def _quiet(self) -> None:
        self._quiet_timer = None
        self._decider.resume(Hold.SPEAKING)

    # -- people and their tracks -------------------------------------------------------

    def _on_joined(self, person: rtc.RemoteParticipant) -> None:
        if self.state.mode == "open" and role_of(person) == "student":
            self._spawn(self._allow_mic(person.identity, True))

    def _on_left(self, person: rtc.RemoteParticipant) -> None:
        self._last_spoke.pop(person.identity, None)
        if any(h["identity"] == person.identity for h in self.state.hands):
            self._spawn(self._hand(person.identity, person.name or "", up=False))
        if person.identity == self._sharing:
            self._spawn(self._share_ended(person.identity))
        # A pause belongs to the staff. With none left, nobody could lift it.
        people = self._room.remote_participants.values()
        if self.state.paused and not any(role_of(p) in STAFF for p in people):
            self._spawn(self._pause(False))

    def _on_track_published(
        self, publication: rtc.RemoteTrackPublication, person: rtc.RemoteParticipant
    ) -> None:
        if person.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            return
        if publication.kind == rtc.TrackKind.KIND_AUDIO:
            # Hearing people is how we know to stop for them (and later, what they said).
            publication.set_subscribed(True)
        elif publication.source == rtc.TrackSource.SOURCE_SCREENSHARE and role_of(person) in STAFF:
            self._spawn(self._share_began(person))

    def _on_track_unpublished(
        self, publication: rtc.RemoteTrackPublication, person: rtc.RemoteParticipant
    ) -> None:
        if publication.source == rtc.TrackSource.SOURCE_SCREENSHARE:
            self._spawn(self._share_ended(person.identity))

    async def _show(self, rows: list[list[dict[str, Any]]]) -> None:
        await self._events.emit({"type": "stage.show", "rows": rows})
        self._decider.stage_changed(rows)  # the lecturer's own screen may be off stage now

    async def _share_began(self, person: rtc.RemoteParticipant) -> None:
        """A helper or guest shares their screen: it takes the stage."""
        if not self._sharing:
            self._stage_before_share = self._events.stage.snapshot()["rows"]
        self._sharing = person.identity
        pane = {"source": "share", "who": person.identity, "name": person.name or "Guest"}
        await self._show([[pane]])

    async def _share_ended(self, identity: str) -> None:
        if identity != self._sharing:
            return
        self._sharing = ""
        rows = self._events.stage.rows
        still_showing = any(p.get("who") == identity for row in rows for p in row)
        if still_showing and self._stage_before_share:
            await self._show(self._stage_before_share)
        self._stage_before_share = None
