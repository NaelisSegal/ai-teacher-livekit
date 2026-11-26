"""An AI student who joins a real class, for trying a lesson with a person and an AI.

It joins the room like a browser would, follows what the lecturer says (as the
captions), what is on the board and what is typed in the chat, and a model plays
the student it is told to be: it types in the chat and presses done, as that
student would (see `dev/eval/students.py`). It does not listen to the audio or
speak. The model is CH_STUDENTS in agent/.env.local.

    cd agent && uv run python ../dev/eval/ai_student.py \\
        <room> --name Sam --persona "An adult who ..."

    --persona-file <file>   the description in a file instead
    --lesson <course>/<lesson>   also send the lecturer, if nobody has yet (as the
                                 class page does when the first person joins)

The room is the one in the class page's address: /class/<room>. Ctrl-C to leave.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from livekit import rtc

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dev" / "eval"))
sys.path.insert(0, str(ROOT / "dev" / "checks"))  # the scripted student it builds on
load_dotenv(ROOT / "agent" / ".env.local")

from student_bot import Student, ensure_lecturer  # noqa: E402
from students import SimStudent, describe_board  # noqa: E402

from lecturer.room.board import Board  # noqa: E402
from lecturer.room.floor import FloorState  # noqa: E402
from lecturer.room.stage import Stage  # noqa: E402
from lecturer.settings import model_for  # noqa: E402

CHAT_TOPIC = "ch.chat"
REACT_TOPIC = "ch.react"
EVERY = 15  # outside tasks, the student is asked after this many sentences


class AIStudent(Student):
    def __init__(self, name: str, persona: str) -> None:
        super().__init__(name)
        model = model_for("students")
        if model is None:
            sys.exit("no model for the student: put CH_STUDENTS in agent/.env.local")
        self.sim = SimStudent(name, persona, model)
        self.heard: list[str] = []
        self.chat: list[str] = []
        self.since = 0
        self.last_task = ""
        self.turn: asyncio.Task[None] | None = None
        self.began: float | None = None  # when the lecturer first spoke
        self.seen_split: dict[str, Any] | None = None
        self.going = ""  # a room the student is on the way to
        self.home = ""  # the class's own room
        self.here = ""  # the room the student is in now: the class's, or a group's

    async def join(self, room_name: str) -> None:
        self.home = self.home or room_name
        self.here = room_name
        self.room.register_text_stream_handler(
            CHAT_TOPIC, lambda reader, who: self._spawn(self._on_chat(reader, who))
        )
        await super().join(room_name)

    def _move(self, room_name: str) -> None:
        """Go to another room (a group, or back to the class), as a browser does."""
        if room_name and room_name != self.here and room_name != self.going:
            self.going = room_name
            self._spawn(self._go(room_name))

    async def _go(self, room_name: str) -> None:
        await asyncio.sleep(1.5)  # as the class page waits, so the move can be seen
        await self.room.disconnect()
        self.room = rtc.Room()
        self.board, self.stage, self.floor = Board(), Stage(), FloorState()
        self.last_seq, self.synced, self.buffer, self.snapshot = 0, False, {}, None
        print(f"moved to {room_name}", flush=True)
        await self.join(room_name)
        self.going = ""

    def _my_group(self, split: dict[str, Any], group_id: str) -> str:
        return next((g["room"] for g in split.get("groups", []) if g["id"] == group_id), "")

    async def _choose(self, split: dict[str, Any]) -> None:
        title = await self.sim.choose(split.get("groups", []), self.heard)
        group = next(g for g in split["groups"] if g["title"] == title)
        print(f"chose the group: {title}", flush=True)
        self._move(group["room"])

    async def _on_chat(self, reader: rtc.TextStreamReader, who: str) -> None:
        text = (await reader.read_all()).strip()
        person = self.room.remote_participants.get(who)
        self.chat.append(f"{person.name if person and person.name else who}: {text}")

    def _apply(self, ev: dict[str, Any]) -> None:
        super()._apply(ev)
        t = ev["type"]
        me = self.room.local_participant.identity
        if t == "split.open":
            to = (ev.get("to") or {}).get(me)
            if to:
                self._move(self._my_group(ev, to))
            elif ev.get("how") == "choose":
                self._spawn(self._choose(ev))
        elif t == "split.assign" and ev.get("identity") == me and self.stage is not None:
            split = self.seen_split or {}
            self._move(self._my_group(split, ev["group"]))
        elif t == "split.back":
            self._move(ev.get("room", self.home))
        if t == "split.open":
            self.seen_split = ev
        if t == "say" and ev.get("text"):
            if self.began is None:
                self.began = asyncio.get_running_loop().time()
            self.heard.append(ev["text"])
            self.since += 1
            if self.since >= EVERY:
                self._ask(
                    "The teacher is talking, and has not set a task. Most of the time a "
                    "student types nothing now, unless they have a question or are lost.",
                    20,
                )
        # A pause is sent again when it carries on after a hold: one task, once.
        elif t == "pause" and not ev.get("watching") and ev.get("text", "") != self.last_task:
            self.last_task = ev.get("text", "")
            self._ask(
                f'The teacher has set the class a task: "{self.last_task}". The class '
                f"has {ev['seconds']:g} seconds for it.",
                float(ev["seconds"]),
            )

    def _ask(self, now: str, seconds: float) -> None:
        self.since = 0
        if self.turn is not None and not self.turn.done():
            self.turn.cancel()  # something new has happened: decide again
        self.turn = asyncio.create_task(self._act(now, seconds))

    async def _act(self, now: str, seconds: float) -> None:
        board = describe_board(self.board)
        if self.began is None:
            self.began = asyncio.get_running_loop().time()
        minutes = (asyncio.get_running_loop().time() - self.began) / 60
        now = f"About {minutes:.0f} minutes into the class. {now}"
        actions = await self.sim.decide(now, self.heard, board, self.chat)
        loop = asyncio.get_running_loop()
        began = loop.time()
        for action in sorted(actions, key=lambda a: a.after):
            if action.after > seconds + 20:
                continue
            await asyncio.sleep(max(0.0, action.after - (loop.time() - began)))
            if action.chat:
                self.sim.typed.append(action.chat)
                self.chat.append(f"{self.name} (you): {action.chat}")
                await self.room.local_participant.send_text(action.chat, topic=CHAT_TOPIC)
                print(f"typed: {action.chat}", flush=True)
            if action.done:
                await self.room.local_participant.send_text("done", topic=REACT_TOPIC)
                print("pressed done", flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("room")
    ap.add_argument("--name", default="Sam")
    ap.add_argument("--persona", default="")
    ap.add_argument("--persona-file", default="")
    ap.add_argument("--lesson", default="")
    args = ap.parse_args()
    persona = Path(args.persona_file).read_text() if args.persona_file else args.persona
    if not persona.strip():
        sys.exit("say who the student is: --persona or --persona-file")
    if args.lesson:
        # As the class page does in development: a live- room is taught live.
        meta = {"lesson": args.lesson, "live": args.room.startswith("live-")}
        await ensure_lecturer(args.room, meta)
    student = AIStudent(args.name, persona.strip())
    await student.join(args.room)
    print(f"{args.name} has joined {args.room}", flush=True)
    with contextlib.suppress(asyncio.CancelledError):
        while True:
            await student.ended.wait()
            # A group ending is not the class ending: only the class's own room counts.
            await asyncio.sleep(3)
            if student.here == student.home and not student.going and student.phase == "ended":
                break
            student.ended.clear()
    print(json.dumps({"model calls": student.sim.calls, "typed": student.sim.typed}))
    await student.room.disconnect()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
