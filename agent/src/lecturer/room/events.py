"""The class event stream, live state, and the event log.

Everything students see or hear in order (captions, board and stage changes, pauses,
plan position) is one stream of numbered events on the `ch.class` topic. Browsers apply
them in sequence order. A browser that joins, reconnects or notices a gap calls the
`ch.sync` RPC and is sent a snapshot of live state with the sequence number it is
good up to.

Live state is in memory. The log on disk is a copy: one JSON object per line. It
records what students heard and saw, never what they said or typed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from livekit import rtc

from .board import Board
from .floor import STAFF, FloorState, role_of
from .polls import PollState
from .stage import Stage

logger = logging.getLogger("lecturer.events")

CLASS_TOPIC = "ch.class"
SYNC_RPC = "ch.sync"
TICK_SECONDS = 5
SYNC_EVERY_SECONDS = 1.0  # one snapshot per student per second, at most


def is_private(ev: dict[str, Any]) -> bool:
    """An event that only staff may see in full: a helper being asked whether a
    student may be taken aside (who, and the teacher's reason), and the aside itself."""
    t = ev.get("type")
    return t == "split.ask" or (t == "split.open" and ev.get("how") == "one")


def private_split(split: dict[str, Any] | None, identity: str | None) -> dict[str, Any] | None:
    """The class's split as a student sees it. Of one student taken aside, only that
    student is told (where to go); to the others it is as if nothing had happened."""
    if not split or split.get("how") != "one":
        return split
    to = split.get("to") or {}
    if identity in to:
        mine = to[identity]
        return {
            **split,
            "to": {identity: mine},
            "groups": [
                {k: v for k, v in g.items() if k != "about"}
                for g in split["groups"]
                if g["id"] == mine
            ],
        }
    return None


def for_student(ev: dict[str, Any], identity: str | None) -> dict[str, Any]:
    """An event as a student is sent it: in full, unless only staff may see it."""
    if not is_private(ev):
        return ev
    if ev["type"] == "split.ask":
        return {"type": "noop", "seq": ev["seq"], "t": ev["t"]}
    seen = private_split(ev, identity)
    if seen is None:
        return {"type": "noop", "seq": ev["seq"], "t": ev["t"]}
    return {**ev, "to": seen["to"], "groups": seen["groups"]}


def for_the_log(ev: dict[str, Any]) -> dict[str, Any]:
    """An event as the log keeps it: what happened, without who.

    Browsers need to know which student raised a hand or has the floor. The log does
    not: it keeps what students heard and saw, not what they did. So names and
    identities are left out, and the line that invites a student by name is kept
    without the name.
    """
    t = ev.get("type", "")
    if t == "split.ask" or (t == "split.open" and ev.get("how") == "one"):
        # That a student was taken aside, but not who, or why.
        return {
            k: v for k, v in ev.items() if k in ("type", "seq", "t", "id", "ok", "how", "minutes")
        }
    if t.startswith(("floor.", "split.")):
        # Not who raised a hand, or who went to which group.
        return {k: v for k, v in ev.items() if k not in ("identity", "name", "to")}
    if t == "say" and ev.get("source") == "invite":
        return {**ev, "text": "(a student's name), go ahead."}
    if t == "stage.show":
        rows = [
            [{k: v for k, v in p.items() if k not in ("who", "name")} for p in r]
            for r in ev["rows"]
        ]
        return {**ev, "rows": rows}
    return ev


class ClassEvents:
    def __init__(self, room: rtc.Room, log_path: Path | None = None) -> None:
        self._room = room
        self._seq = 0
        # Names this run of the lecturer. Numbers start again after a restart, and
        # browsers use this to know they must start again too.
        self.epoch = uuid.uuid4().hex[:8]
        self._last_sync: dict[str, float] = {}
        self._ticker: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._log = None
        self._tasks: set[asyncio.Task[None]] = set()
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = log_path.open("a", buffering=1)

        # live state: what a snapshot is made from
        self.board = Board()
        self.stage = Stage()
        self.floor = FloorState()  # who may speak, raised hands, who has the floor
        self.polls = PollState()
        self.caption: dict[str, Any] | None = None  # the sentence being spoken now
        # The lesson's last few sentences, as context for answering a question. Lines
        # that name a student ("Ada, go ahead") are not kept here.
        self.said: deque[str] = deque(maxlen=14)
        self.plan: dict[str, Any] = {}  # segment id, title, step number
        self.pause: dict[str, Any] | None = None  # a thinking pause in progress
        self.phase = "waiting"  # waiting | teaching | ended
        # The class is in groups (`groups.py`): the groups, how students come to be in
        # one, and who has been put where. None when it is not.
        self.split: dict[str, Any] | None = None
        self.asks: list[dict[str, Any]] = []  # what helpers are being asked to agree to
        # In a group's own room: which group this is and the room it came from. `back`
        # once its students are to go back there.
        self.group: dict[str, Any] | None = None

    def register(self) -> None:
        self._room.local_participant.register_rpc_method(SYNC_RPC, self._on_sync)
        self._ticker = asyncio.create_task(self._tick())

    async def _tick(self) -> None:
        """Say every few seconds how far we have got, so that a browser which missed
        the last event (with nothing after it to show the gap) notices."""
        while True:
            await asyncio.sleep(TICK_SECONDS)
            tick = json.dumps({"type": "tick", "epoch": self.epoch, "seq": self._seq})
            with contextlib.suppress(Exception):
                await self._room.local_participant.send_text(tick, topic=CLASS_TOPIC)

    # -- who may see what -----------------------------------------------------------

    def _is_staff(self, identity: str) -> bool:
        return role_of(self._room.remote_participants.get(identity)) in STAFF

    # -- live state ---------------------------------------------------------------

    def snapshot(self, for_identity: str | None = None) -> dict[str, Any]:
        """Live state. For a student (`for_identity`), without what only staff may see:
        what helpers are being asked to agree to, and who has been taken aside."""
        staff = for_identity is None or self._is_staff(for_identity)
        pause = None
        if self.pause:
            left = self.pause["until"] - time.time()
            if left > 0:
                pause = {
                    "seconds_left": round(left, 1),
                    "text": self.pause["text"],
                    "watching": self.pause["watching"],
                }
        return {
            "type": "snapshot",
            "epoch": self.epoch,
            "seq": self._seq,
            "phase": self.phase,
            "plan": self.plan,
            "board": self.board.snapshot(),
            "stage": self.stage.snapshot(),
            "floor": self.floor.snapshot(),
            "polls": self.polls.snapshot(),
            "caption": self.caption,
            "pause": pause,
            "split": self.split if staff else private_split(self.split, for_identity),
            "asks": self.asks if staff else [],
            "group": self.group,
        }

    def _reduce(self, ev: dict[str, Any]) -> None:
        t = ev["type"]
        if t.startswith("board."):
            self.board.apply(ev)
        elif t.startswith(("stage.", "slides.", "screen.", "video.")):
            self.stage.apply(ev)
        elif t.startswith("floor."):
            self.floor.apply(ev)
        elif t.startswith("poll."):
            self.polls.apply(ev)
        elif t == "say":
            self.caption = {"id": ev["id"], "text": ev["text"]}
            if ev.get("source") not in ("ready", "invite"):
                self.said.append(ev["text"])
            self.pause = None
        elif t in ("heard", "cut"):
            if self.caption and self.caption["id"] == ev["id"]:
                self.caption = None
        elif t == "pause":
            self.pause = {
                "until": time.time() + ev["seconds"],
                "text": ev.get("text", ""),
                "watching": bool(ev.get("watching")),
            }
        elif t == "pause.end":
            self.pause = None
        elif t == "plan":
            self.plan = {k: v for k, v in ev.items() if k not in ("type", "seq", "t")}
        elif t == "phase":
            self.phase = ev["phase"]
        elif t == "split.open":
            self.split = {
                "how": ev["how"],
                "minutes": ev.get("minutes", 0),
                "groups": [{**g, "ended": False} for g in ev["groups"]],
                "to": dict(ev.get("to") or {}),
            }
        elif t == "split.assign" and self.split:
            self.split["to"][ev["identity"]] = ev["group"]
        elif t == "split.ended" and self.split:
            for group in self.split["groups"]:
                if group["id"] == ev["group"]:
                    group["ended"] = True
        elif t == "split.close":
            self.split = None
        elif t == "split.ask":
            self.asks.append({k: ev[k] for k in ("id", "identity", "name", "title", "why")})
        elif t == "split.asked":
            self.asks = [ask for ask in self.asks if ask["id"] != ev["id"]]
        elif t == "split.back" and self.group:
            self.group["back"] = True

    # -- sending ------------------------------------------------------------------

    async def emit(self, *events: dict[str, Any]) -> None:
        """Number, apply, log and broadcast events, in that order.

        Events given together are sent as one message, so a sentence and its board
        changes arrive at the same moment.
        """
        async with self._send_lock:
            now = round(time.time(), 3)
            for ev in events:
                self._seq += 1
                ev["seq"] = self._seq
                ev["t"] = now
                self._reduce(ev)
                self._write_log(ev)
            for payload, to in self._payloads(list(events)):
                try:
                    await self._room.local_participant.send_text(
                        payload, topic=CLASS_TOPIC, destination_identities=to
                    )
                except Exception as e:
                    # Students who missed this will see a gap and ask for a snapshot.
                    logger.warning("could not send events up to seq %d: %s", self._seq, e)

    def _payloads(self, events: list[dict[str, Any]]) -> list[tuple[str, list[str] | None]]:
        """What to send, and to whom (None: everyone). Mostly one message for everyone.
        An event only staff may see in full goes in full to staff; the students get it
        blanked, in its place, so that its number is not missing for them."""

        def message(evs: list[dict[str, Any]]) -> str:
            return json.dumps({"type": "events", "epoch": self.epoch, "events": evs})

        if not any(is_private(ev) for ev in events):
            return [(message(events), None)]
        people = list(self._room.remote_participants.values())
        staff = [p.identity for p in people if role_of(p) in STAFF]
        students = [p.identity for p in people if role_of(p) not in STAFF]
        out: list[tuple[str, list[str] | None]] = []
        if staff:
            out.append((message(events), staff))
        # The student taken aside is told where to go; nobody else is told anything.
        named = {who for ev in events if is_private(ev) for who in (ev.get("to") or {})}
        for who in [s for s in students if s in named]:
            out.append((message([for_student(ev, who) for ev in events]), [who]))
        rest = [s for s in students if s not in named]
        if rest:
            out.append((message([for_student(ev, None) for ev in events]), rest))
        return out

    def note(self, what: str, /, **fields: Any) -> None:
        """Write a log-only line (timings, internal decisions). Never sent to students.

        `what` can only be given by position, so a field may have any name.
        """
        self._write_log({"type": f"note.{what}", "t": round(time.time(), 3), **fields})

    def _write_log(self, ev: dict[str, Any]) -> None:
        if self._log:
            self._log.write(json.dumps(for_the_log(ev), ensure_ascii=False) + "\n")

    async def _on_sync(self, data: rtc.RpcInvocationData) -> str:
        """A browser asks for a snapshot. Reply at once, then send it to them alone."""
        now = time.monotonic()
        if now - self._last_sync.get(data.caller_identity, 0) < SYNC_EVERY_SECONDS:
            return json.dumps({"seq": self._seq, "wait": True})
        self._last_sync[data.caller_identity] = now
        snap = self.snapshot(data.caller_identity)
        # Written out now, while it is what its sequence number says it is.
        written = json.dumps(snap)

        async def send() -> None:
            try:
                await self._room.local_participant.send_text(
                    written,
                    topic=CLASS_TOPIC,
                    destination_identities=[data.caller_identity],
                )
            except Exception:
                logger.exception("could not send a snapshot")  # no names in the process log

        task = asyncio.create_task(send())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return json.dumps({"seq": snap["seq"]})

    def close(self) -> None:
        if self._ticker:
            self._ticker.cancel()
            self._ticker = None
        if self._log:
            self._log.close()
            self._log = None
