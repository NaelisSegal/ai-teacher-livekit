"""Reactions: a thumbs up, a thumbs down, or "done", given in the moment.

A reaction is not class state. A student's browser sends it to the room, every
browser shows it for a few seconds, and it is gone. It is not in the event stream,
the snapshot, or the log (the log notes only that one came, not from whom).

The lecturer notices them too, quietly. For now it uses one thing: when every
student has pressed done during a thinking pause, the pause ends early.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from livekit import rtc

from .floor import person_of, role_of
from .streams import read_capped, refuse

if TYPE_CHECKING:
    from ..speech.decider import VoiceDecider
    from .events import ClassEvents

logger = logging.getLogger("lecturer.react")

REACT_TOPIC = "ch.react"
KINDS = ("up", "down", "done")
ALL_DONE_GRACE_SECONDS = 1.0


class Reactions:
    def __init__(self, room: rtc.Room, events: ClassEvents, decider: VoiceDecider) -> None:
        self._room = room
        self._events = events
        self._decider = decider
        self._done: set[str] = set()  # who has pressed done since the task began
        self._seen: list[tuple[float, str, str]] = []  # when, kind, who: the last minute
        self._tasks: set[asyncio.Task[Any]] = set()
        # Told of every reaction: (who, kind). The live teacher listens here.
        self.listener: Callable[[str, str], None] | None = None

    def register(self) -> None:
        self._room.register_text_stream_handler(REACT_TOPIC, self._on_stream)
        self._room.on("participant_disconnected", lambda p: self._done.discard(p.identity))

    def begin_task(self) -> None:
        """A thinking pause starts: forget who was done with the last one."""
        self._done.clear()

    def recent(self, kind: str, seconds: float) -> int:
        """How many different people gave this reaction in the last while."""
        since = time.monotonic() - seconds
        return len({who for at, k, who in self._seen if k == kind and at >= since})

    def _students(self) -> set[str]:
        agent = rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        people = self._room.remote_participants.values()
        return {person_of(p) for p in people if p.kind != agent and role_of(p) == "student"}

    def _on_stream(self, reader: rtc.TextStreamReader, identity: str) -> None:
        task = asyncio.create_task(self._read(reader, identity))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read(self, reader: rtc.TextStreamReader, identity: str) -> None:
        now = time.monotonic()
        self._seen = [s for s in self._seen if now - s[0] < 60]
        if sum(1 for at, _, who in self._seen if who == identity and now - at < 5) >= 6:
            refuse(reader)  # too fast
            return
        kind = (await read_capped(reader, 16) or "").strip()
        if kind not in KINDS:
            return
        self._seen.append((now, kind, identity))
        self._events.note("react", reaction=kind)
        if self.listener is not None:
            self.listener(identity, kind)
        if kind == "done" and self.listener is None:
            # (The live teacher decides for itself when a wait is over.)
            self._done.add(person_of(self._room.remote_participants.get(identity), identity))
            students = self._students()
            if students and students <= self._done:
                await asyncio.sleep(ALL_DONE_GRACE_SECONDS)
                if self._students() <= self._done:
                    # Every student has finished: no need to wait out the pause.
                    self._decider.end_pause()
