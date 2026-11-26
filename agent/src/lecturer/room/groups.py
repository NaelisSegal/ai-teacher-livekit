"""Groups: the class splits, and each group has its own lecturer.

A group is a room of its own with a lecturer of its own. The main class's lecturer
makes the rooms, sends for a lecturer for each, and tells the browsers; each browser
then moves its student to their group, and back again at the end. (The lecturer
cannot move anyone: a self-hosted LiveKit server has no way to.)

Who goes where is one of:

    choose    each student picks a group, and may change their mind
    random    students are put in groups by chance
    helper    a human helper puts each student in a group
    one       one student is taken aside while the class goes on. The live teacher
              asks for this, and a helper must agree before it happens.

A helper may also move any student, and end any group, at any time.

A group's lecturer either reads a prepared lesson, or teaches live from the class's
own lesson prep with a short brief saying what the group is for. So tracks planned
in lesson prep and a split made up on the spot are the same thing here.

A split is best supervised by people: by default it needs one helper present for each
group (two, to take one student aside, so that the class is not left alone), and
without them it is not done. That is a setting, CH_SPLIT_HELPERS (helpers needed for
each group; 1 if not set, and 1 is recommended). With 0, the class can be split with
nobody to supervise the groups, which is for trying things out. Taking one student
aside always needs a helper to agree to it.

The main lecturer and the group lecturers are in different rooms, so they talk through
each group room's metadata, which LiveKit holds: the group's lecturer writes how it is
going there (what it has said, never what students said), and the main lecturer reads
it and, to end a group, writes `close` there. A group that was made to be private (one
student taken aside) reports only that it is still going.

LiveKit closes a room a few seconds after the last person leaves it, and a lecturer
does not count as a person. When the whole class goes into groups the main room is
left with only its lecturer, so for as long as the class is split the lecturer keeps
the room open with a second, hidden connection of its own (`KEEPER`), which nobody
sees and which is not counted as a student.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from livekit import api, rtc

from ..job import LecturerJob
from ..script.lesson import lesson_file
from ..script.steps import Step
from .events import ClassEvents
from .floor import STAFF, person_of, role_of

logger = logging.getLogger("lecturer.groups")

AGENT_NAME = "lecturer"
GROUP_OF = "ch_group_of"  # in a group room's metadata: the room it split from
SPLIT_RPC = "ch.split"
WATCH_SECONDS = 4.0
KEEPER = "ch-keeper"  # the hidden connection that keeps the main room open
CLOSE_WAIT = 25.0  # how long a group has to wind up before the class goes on without it
ASK_WAIT = 90.0  # how long a helper has to answer before it counts as no


def helpers_per_group() -> int:
    """How many helpers each group needs (CH_SPLIT_HELPERS; 1 if not set)."""
    try:
        return max(0, int(os.environ.get("CH_SPLIT_HELPERS", "1") or 1))
    except ValueError:
        return 1


_LESSON = re.compile(r"^[a-z0-9-]+/[a-z0-9-]+$")


@dataclass
class Group:
    id: str
    title: str
    about: str  # what students are told it is
    lesson: str  # what its lecturer teaches from
    brief: str  # for a group taught live: what it is for
    room: str
    private: bool = False
    ended: bool = False
    seen: bool = False  # its room has been seen to exist
    made_at: float = field(default_factory=time.monotonic)
    meta: dict[str, Any] = field(default_factory=dict)  # its room's metadata, as last read

    @property
    def report(self) -> dict[str, Any]:
        return dict(self.meta.get("status") or {})

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "about": self.about, "room": self.room}


def lecturer_api() -> api.LiveKitAPI:
    return api.LiveKitAPI(os.environ["LIVEKIT_URL"].replace("ws", "http", 1))


class Groups:
    def __init__(
        self,
        room: rtc.Room,
        events: ClassEvents,
        *,
        lesson: str,
        can_teach_live: bool,
        lk: Any = None,
        keep_room: bool = True,
    ) -> None:
        self._room = room
        self._events = events
        self._lesson = lesson
        self._live = can_teach_live
        self._lk = lk
        self._keep_room = keep_room
        self._keeper: rtc.Room | None = None
        self._groups: list[Group] = []
        self._one = False  # the split is one student taken aside
        self._watcher: asyncio.Task[None] | None = None
        self._asks: dict[str, asyncio.Future[bool]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self.away = False  # the whole class is in groups, so the main room is empty
        self.last: list[dict[str, Any]] = []  # how the last groups went, once they are over
        self.on_change: Callable[[], None] | None = None  # people have gone, or are due back
        self.on_done: Callable[[], None] | None = None  # every group has ended
        # Students are on their way back to the main room (True), or are back (False).
        self.on_returning: Callable[[bool], None] | None = None
        self._how = ""
        self._to: dict[str, str] = {}

    def register(self) -> None:
        self._room.local_participant.register_rpc_method(SPLIT_RPC, self._on_staff)
        self._room.on("participant_connected", self._on_joined)

    def _on_joined(self, person: rtc.RemoteParticipant) -> None:
        """Someone who arrives while the class is in groups by chance is put in one."""
        going = [g for g in self._groups if not g.ended]
        if self._how != "random" or not going or person.identity in self._to:
            return
        if person.identity == KEEPER or role_of(person) != "student":
            return
        sizes = {g.id: list(self._to.values()).count(g.id) for g in going}
        self._to[person.identity] = min(sizes, key=lambda g: sizes[g])
        task = asyncio.create_task(
            self._events.emit(
                {
                    "type": "split.assign",
                    "identity": person.identity,
                    "group": self._to[person.identity],
                }
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- who is here ------------------------------------------------------------------

    def _people(self) -> list[rtc.RemoteParticipant]:
        agent = rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        people = self._room.remote_participants.values()
        return [p for p in people if p.kind != agent and p.identity != KEEPER]

    async def _keep(self) -> None:
        """Keep the main room open while nobody but the lecturer is in it."""
        if not self._keep_room or self._keeper is not None:
            return
        token = (
            api.AccessToken()
            .with_identity(KEEPER)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=self._room.name,
                    hidden=True,
                    can_publish=False,
                    can_publish_data=False,
                    can_subscribe=False,
                )
            )
            .to_jwt()
        )
        keeper = rtc.Room()
        try:
            await keeper.connect(
                os.environ["LIVEKIT_URL"], token, rtc.RoomOptions(auto_subscribe=False)
            )
            self._keeper = keeper
        except Exception as e:
            logger.error("could not keep the main room open while the class is split: %s", e)

    async def _let_go(self, after: float = 0.0) -> None:
        await asyncio.sleep(after)
        if after and (self.away or self._groups):
            return  # the class has split again since: the room is still needed
        keeper, self._keeper = self._keeper, None
        if keeper is not None:
            with contextlib.suppress(Exception):
                await keeper.disconnect()

    def helpers(self) -> int:
        return sum(1 for p in self._people() if role_of(p) in STAFF)

    def most_groups(self) -> int:
        """How many groups the class could be split into now."""
        students = len(self._students())
        per = helpers_per_group()
        return students if per == 0 else min(students, self.helpers() // per)

    def _students(self) -> list[rtc.RemoteParticipant]:
        return [p for p in self._people() if role_of(p) == "student"]

    @property
    def open_now(self) -> bool:
        return bool(self._groups)

    def reports(self) -> list[dict[str, Any]]:
        """How each group is going, for whoever is keeping an eye on them."""
        return [
            {
                "title": g.title,
                "for": g.brief or f"the prepared lesson {g.lesson}",
                "ended": g.ended,
                "private": g.private,
                **({} if g.private else g.report),
            }
            for g in self._groups
        ]

    # -- opening ----------------------------------------------------------------------

    def _plan(self, step: Step) -> list[Group] | str:
        """The groups a step asks for, or why they cannot be made."""
        stem = re.sub(r"[^\w-]", "", self._room.name)[:44] or "class"
        out: list[Group] = []
        for n, (title, what) in enumerate(step.groups, 1):
            room = f"{stem}-g{n}-{uuid.uuid4().hex[:4]}"
            prepared = bool(_LESSON.match(what)) and lesson_file(f"{what}.seg").is_file()
            if prepared:
                out.append(Group(f"g{n}", title, "", what, "", room))
            elif not self._live:
                return f'the group "{title}" would be taught live, and there is no writer model'
            else:
                out.append(Group(f"g{n}", title, what, self._lesson, what, room))
        return out

    async def _make(self, group: Group, minutes: float, said: list[str]) -> None:
        if self._lk is None:
            self._lk = lecturer_api()
        group.meta = {GROUP_OF: self._room.name, "title": group.title}
        if group.private:
            # Only this student (and staff) may join it: the web app checks.
            # As people, which is what the web app checks a join against.
            people = self._room.remote_participants
            group.meta["only"] = sorted({person_of(people.get(i), i) for i in self._to})
        await self._lk.room.create_room(
            api.CreateRoomRequest(
                name=group.room,
                metadata=json.dumps(group.meta),
                empty_timeout=180,
                departure_timeout=30,
            )
        )
        job = LecturerJob(
            lesson=group.lesson,
            live=bool(group.brief),
            group={
                "id": group.id,
                "title": group.title,
                "brief": group.brief,
                "main": self._room.name,
                "minutes": minutes,
                "said": said[-8:],
                "private": group.private,
            },
        )
        await self._lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME, room=group.room, metadata=job.to_json()
            )
        )

    async def open(self, step: Step, said: list[str]) -> str:
        """Split the whole class. "" if it was done, or why it was not."""
        if self._groups:
            return "the class is already in groups"
        planned = self._plan(step)
        if isinstance(planned, str):
            return planned
        need = helpers_per_group() * len(planned)
        if self.helpers() < need:
            return (
                f"{len(planned)} groups need {need} human helpers here, "
                f"and there {'is' if self.helpers() == 1 else 'are'} {self.helpers()}"
            )
        try:
            for group in planned:
                await self._make(group, step.seconds / 60, said)
        except Exception as e:
            logger.exception("could not make the groups")
            return f"the group rooms could not be made ({type(e).__name__})"
        to: dict[str, str] = {}
        if step.style == "random":
            # By person, so that someone here in two tabs goes to one group in both.
            people = sorted({person_of(p) for p in self._students()})
            random.shuffle(people)
            group_of = {who: planned[n % len(planned)].id for n, who in enumerate(people)}
            to = {p.identity: group_of[person_of(p)] for p in self._students()}
        self._groups, self._one, self.away = planned, False, True
        self._how, self._to = step.style, dict(to)
        await self._keep()
        await self._events.emit(
            {
                "type": "split.open",
                "how": step.style,
                "minutes": round(step.seconds / 60, 1),
                "groups": [g.public() for g in planned],
                "to": to,
            }
        )
        self._watch()
        return ""

    async def aside(self, step: Step, said: list[str]) -> str:
        """Take one student aside, if a helper agrees. "" if it was done."""
        # The teacher names a person; any tab of theirs will do to find them.
        who = next((p for p in self._students() if person_of(p) == step.text), None)
        if self._groups:
            return "the class is already in groups"
        if who is None or role_of(who) != "student":
            return "that student is not here"
        need = 2 if helpers_per_group() else 1  # someone must agree to it
        if self.helpers() < need:
            return f"taking a student aside needs {need} human helper{'s' if need > 1 else ''} here"
        planned = self._plan(step)
        if isinstance(planned, str):
            return planned
        (group,) = planned
        group.private = True
        ask = uuid.uuid4().hex[:8]
        answer: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._asks[ask] = answer
        asked = {"id": ask, "identity": who.identity, "name": who.name or "", "title": group.title}
        await self._events.emit({"type": "split.ask", **asked, "why": group.brief})
        try:
            agreed = await asyncio.wait_for(answer, ASK_WAIT)
        except TimeoutError:
            agreed = False
        finally:
            self._asks.pop(ask, None)
        await self._events.emit({"type": "split.asked", "id": ask, "ok": agreed})
        if not agreed:
            return "no helper agreed to it"
        tabs = [p for p in self._students() if person_of(p) == step.text]
        if self._groups or not tabs:
            return "things changed while a helper was deciding"
        self._how, self._to = "one", {p.identity: group.id for p in tabs}
        try:
            await self._make(group, step.seconds / 60, said)
        except Exception as e:
            logger.exception("could not make a room to take a student aside")
            return f"the room could not be made ({type(e).__name__})"
        self._groups, self._one = [group], True
        await self._events.emit(
            {
                "type": "split.open",
                "how": "one",
                "minutes": round(step.seconds / 60, 1),
                "groups": [group.public()],
                # Every tab of theirs goes (someone may be here in two).
                "to": {
                    p.identity: group.id for p in self._students() if person_of(p) == person_of(who)
                },
            }
        )
        self._watch()
        return ""

    # -- watching and closing ---------------------------------------------------------

    def _watch(self) -> None:
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.create_task(self._watching())

    async def _watching(self) -> None:
        """Read how each group is going, and notice when one has ended."""
        while any(not g.ended for g in self._groups):
            await asyncio.sleep(WATCH_SECONDS)
            going = [g for g in self._groups if not g.ended]
            try:
                found = await self._lk.room.list_rooms(
                    api.ListRoomsRequest(names=[g.room for g in going])
                )
            except Exception as e:
                logger.warning("could not look at the group rooms: %s", e)
                continue
            rooms = {r.name: r for r in found.rooms}
            for group in going:
                there = rooms.get(group.room)
                if there is not None:
                    group.seen = True
                    with contextlib.suppress(ValueError):
                        group.meta = json.loads(there.metadata or "{}")
                age = time.monotonic() - group.made_at
                gone = there is None and (group.seen or age > 30)
                # A group's lecturer reports as soon as it arrives, before any student
                # does. One that has never reported has no lecturer.
                silent = not group.report and age > 45
                if gone or silent or group.report.get("phase") == "ended":
                    group.ended = True
                    await self._events.emit({"type": "split.ended", "group": group.id})
        await self._finish()

    async def _finish(self) -> None:
        if not self._groups:
            return
        self.last = self.reports()
        whole_class = not self._one
        self._groups, self.away, self._how, self._to = [], False, "", {}
        if whole_class and self.on_returning is not None:
            # Students are leaving their groups now. Give them a moment to arrive.
            self.on_returning(True)
            back = asyncio.create_task(self._returned(after=5))
            self._tasks.add(back)
            back.add_done_callback(self._tasks.discard)
        # Students are on their way back. The room is held open a little longer for them.
        task = asyncio.create_task(self._let_go(after=20))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        await self._events.emit({"type": "split.close"})
        for tell in (self.on_done, self.on_change):
            if tell is not None:
                tell()

    async def _returned(self, after: float) -> None:
        await asyncio.sleep(after)
        if self.on_returning is not None and not self.away:
            self.on_returning(False)

    def coming_back(self) -> None:
        """The groups are being ended: from now, an empty main room is a room to wait in."""
        if self.away:
            self.away = False
            if self.on_returning is not None:
                self.on_returning(True)
            if self.on_change is not None:
                self.on_change()

    async def close(self, title: str = "") -> None:
        """End the groups (or the one with this title) and bring their students back."""
        wanted = [
            g
            for g in self._groups
            if not g.ended and (not title or g.title.casefold() == title.strip().casefold())
        ]
        if not wanted:
            return
        if len(wanted) == sum(1 for g in self._groups if not g.ended):
            self.coming_back()
        for group in wanted:
            try:
                await self._lk.room.update_room_metadata(
                    api.UpdateRoomMetadataRequest(
                        room=group.room, metadata=json.dumps({**group.meta, "close": True})
                    )
                )
            except Exception as e:
                # Its room has gone already, or cannot be reached: either way it is over.
                logger.warning("could not tell a group to end: %s", e)
                group.ended = True
        # Its lecturer says goodbye and sends its students back. If it does not, go on.
        began = time.monotonic()
        while any(not g.ended for g in wanted) and time.monotonic() - began < CLOSE_WAIT:
            await asyncio.sleep(0.5)
        for group in wanted:
            if not group.ended:
                group.ended = True
                await self._events.emit({"type": "split.ended", "group": group.id})
        if all(g.ended for g in self._groups):
            await self._finish()

    # -- what helpers may do ----------------------------------------------------------

    async def _on_staff(self, data: rtc.RpcInvocationData) -> str:
        caller = self._room.remote_participants.get(data.caller_identity)
        if role_of(caller) not in STAFF:
            raise rtc.RpcError(403, "only staff can do this")
        try:
            asked = json.loads(data.payload)
            action = asked["action"]
        except (ValueError, KeyError, TypeError):
            raise rtc.RpcError(400, "bad request") from None
        if action == "answer":
            waiting = self._asks.get(str(asked.get("id")))
            if waiting is not None and not waiting.done():
                waiting.set_result(bool(asked.get("ok")))
        elif action == "assign":
            group = next((g for g in self._groups if g.id == asked.get("group")), None)
            who = self._room.remote_participants.get(str(asked.get("identity")))
            if group is None or group.ended or who is None or self._one:
                raise rtc.RpcError(400, "no such student or group")
            self._to[who.identity] = group.id
            await self._events.emit(
                {"type": "split.assign", "identity": who.identity, "group": group.id}
            )
        elif action == "close":
            task = asyncio.create_task(self.close(str(asked.get("title") or "")))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            raise rtc.RpcError(400, "unknown action")
        return "ok"

    async def shut(self) -> None:
        if self._watcher is not None:
            self._watcher.cancel()
        await self._let_go()
        if self._lk is not None:
            with contextlib.suppress(Exception):
                await self._lk.aclose()
