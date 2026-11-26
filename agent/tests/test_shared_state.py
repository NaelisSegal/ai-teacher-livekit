"""The lecturer and the browser apply the same events by the same rules, in two
languages (`room/board.py`, `stage.py`, `floor.py`, `polls.py` here, and
`web/lib/classroom/store.ts`). These recordings keep the two in step.

Each recording in `shared/` is the events a short test lesson sends, and the state
they leave here. This test checks that the lecturer still comes to that state; the
web app's `store.test.ts` checks that a browser given the same events comes to the
same state. If the rules change on purpose, record again:

    cd agent && CH_RECORD_STATE=1 uv run pytest -q tests/test_shared_state.py

and the web test then shows whether the browser follows.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from fakes import PretendRoom, PretendVoice

from lecturer.room import polls
from lecturer.room.events import ClassEvents
from lecturer.room.polls import Polls
from lecturer.script.lesson import parse_segment
from lecturer.speech.decider import VoiceDecider

ROOT = Path(__file__).resolve().parents[2]
LESSONS = ROOT / "dev" / "checks" / "lessons" / "test"
SHARED = Path(__file__).resolve().parent / "shared"
RECORDED = ("pages", "layout", "floor")  # test lessons with no shared screen or video
PARTS = ("board", "stage", "floor", "polls")

POLL = """---
id: polls
course: test
title: Polls
---
title: Polls
say: A question for you.
poll: Which is bigger? | One | Two | Three
pause: 1 Vote.
poll: close
say: Thank you.
"""


STAGE = """---
id: stage
course: test
title: Stage
---
title: Stage
say: First the board alone.
slides: test/stage
show: slides | board
say: Now the slides beside it.
slide: next
say: The next slide.
show: board
say: And the board again.
"""

# Who may speak, as the floor sends it (a script cannot raise a hand).
FLOOR = [
    {"type": "floor.mode", "mode": "hands", "prompt": ""},
    {"type": "floor.hand", "identity": "ada", "name": "Ada", "up": True},
    {"type": "floor.hand", "identity": "bo", "name": "Bo", "up": True},
    {"type": "floor.give", "identity": "ada", "name": "Ada"},
    {"type": "floor.take", "identity": "ada"},
    {"type": "floor.hand", "identity": "bo", "name": "Bo", "up": False},
    {"type": "floor.mode", "mode": "open", "prompt": "Tell me what you think"},
    {"type": "floor.pause", "on": True},
]


async def run(
    script: str, more: list[dict[str, Any]] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Teach a script with a pretend voice, then send `more`: the state before (what a
    browser joining at the start is sent), the events sent, and the state after."""
    room = PretendRoom()
    events = ClassEvents(room)  # type: ignore[arg-type]
    before = events.snapshot()
    decider = VoiceDecider(PretendVoice(0.05), events, gap_ms=0, pause_scale=0.01)  # type: ignore[arg-type]
    decider.polls = Polls(room, events)  # type: ignore[arg-type]
    voice = asyncio.create_task(decider.run())
    decider.enqueue_segment(parse_segment(script))
    await asyncio.wait_for(decider.wait_until_idle(), 20)
    for ev in more or []:
        await events.emit(dict(ev))
    voice.cancel()
    snapshot = events.snapshot()
    sent = [{k: v for k, v in ev.items() if k != "t"} for ev in room.local_participant.events()]
    return (
        {part: before[part] for part in PARTS},
        sent,
        {part: snapshot[part] for part in PARTS},
    )


def lessons() -> dict[str, tuple[str, list[dict[str, Any]]]]:
    found = {name: ((LESSONS / f"{name}.seg").read_text(), []) for name in RECORDED}
    return {**found, "polls": (POLL, []), "stage": (STAGE, FLOOR)}


@pytest.mark.parametrize("name", [*RECORDED, "polls", "stage"])
async def test_the_lecturer_comes_to_the_recorded_state(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CH_LESSONS_DIR", str(LESSONS.parent))
    # Poll ids are random in a class; here they are the same every time.
    numbers = iter(range(1, 1000))
    monkeypatch.setattr(polls.uuid, "uuid4", lambda: uuid.UUID(int=next(numbers)))
    script, more = lessons()[name]
    start, sent, state = await run(script, more)
    path = SHARED / f"{name}.json"
    if os.environ.get("CH_RECORD_STATE"):
        SHARED.mkdir(exist_ok=True)
        record = {"start": start, "events": sent, "state": state}
        path.write_text(json.dumps(record, indent=1) + "\n")
    recorded = json.loads(path.read_text())
    assert start == recorded["start"]
    assert [e["type"] for e in sent] == [e["type"] for e in recorded["events"]]
    assert state == recorded["state"]
