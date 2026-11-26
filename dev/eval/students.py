"""Pretend students played by a model, for simulated classes.

Each student has a description: who they are, how much maths they know, how they
behave in class, the mistakes they tend to make. When the teacher sets the class a
task, and now and then while it talks, the student's model is shown what this
student has just heard and seen, and decides what the student does: type in the
chat, press done, or nothing. Unlike a fixed list of events, what they do follows
what the teacher actually does.

The model is a setting, like the lecturer's own: CH_STUDENTS=provider:model in
agent/.env.local (a small, quick model is enough). Its replies are never shown to
the teacher as anything but chat, reactions and votes, as a real student's would be.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from lecturer.models.providers import Model, ModelError, parse_json

logger = logging.getLogger("sim.students")

SYSTEM = """You play one student in a live online class, so that an AI teacher can be
tested. You are not the teacher's helper: you are this student, and you do what
this student would do, no more and no less.

The student:
{persona}

You are shown what the teacher has said lately, what is on the board, the chat,
and what the student has typed so far. Then you are told what is happening now:
the teacher has set a task, or is just talking. Decide what the student does.

- Work out tasks the way this student would, at their level: right if they would
  get it right, wrong in the way they would get it wrong, or stuck. Real students
  often type only part of their working, or a bare number, or "not sure". A
  student described as rusty or weak really does make mistakes and get stuck,
  often; do not let them be better than described just because you know the
  answer.
- Keep to how often the description says this student types. Most real students
  type at most one or two messages for a task, and nothing at all for many tasks.
- While the teacher is talking and has set no task, nearly always do nothing.
  Type only if this student would really interrupt: lost, or a real question.
  Never type back what the teacher has just explained ("ok so that makes sense").
- Never repeat what the student has already typed.
- Times are in seconds from now. Within a task, act before its time is up, or
  not at all. A student who has finished may press done, once.

Reply with JSON only:
{{"actions": [{{"after": <seconds>, "chat": "<what they type>"}}, {{"after": <seconds>, "done": true}}]}}
An empty list means the student does nothing."""


@dataclass
class Action:
    after: float
    chat: str = ""
    done: bool = False


@dataclass
class SimStudent:
    handle: str  # as the teacher knows them: s1, s2 ...
    persona: str
    model: Model
    typed: list[str] = field(default_factory=list)  # what this student has typed
    calls: int = 0

    async def decide(self, now: str, heard: list[str], board: str, chat: list[str]) -> list[Action]:
        """What this student does next, given what is happening `now`."""
        situation = "\n\n".join(
            [
                "The teacher lately said:\n" + "\n".join(f"- {t}" for t in heard[-25:]),
                "On the board:\n" + (board or "(empty)"),
                "The chat lately:\n" + ("\n".join(chat[-10:]) or "(nothing)"),
                "What you have typed so far:\n"
                + ("\n".join(f"- {t}" for t in self.typed) or "(nothing)"),
                f"Now: {now}",
            ]
        )
        self.calls += 1
        try:
            reply = await self.model.complete(
                SYSTEM.format(persona=self.persona),
                [{"role": "user", "content": situation}],
                max_tokens=600,
                think=False,
                retries=1,
            )
            data = parse_json(reply.text)
        except (ModelError, ValueError) as e:
            logger.warning("student %s could not decide: %s", self.handle, e)
            return []
        out = []
        for item in data.get("actions", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            try:
                after = max(0.0, float(item.get("after", 0)))
            except (TypeError, ValueError):
                continue
            text = str(item.get("chat") or "").strip()
            if text or item.get("done"):
                out.append(Action(after, text, bool(item.get("done"))))
        return out

    async def choose(self, groups: list[dict[str, Any]], heard: list[str]) -> str:
        """Which group this student picks, by its title, when the class splits."""
        options = "\n".join(f"- {g['title']}: {g.get('about', '')}" for g in groups)
        situation = (
            "The teacher lately said:\n"
            + "\n".join(f"- {t}" for t in heard[-15:])
            + f"\n\nThe class is splitting into groups, and you choose one:\n{options}\n\n"
            'Reply with JSON only: {"group": "<the title you choose>"}'
        )
        self.calls += 1
        try:
            reply = await self.model.complete(
                SYSTEM.format(persona=self.persona),
                [{"role": "user", "content": situation}],
                max_tokens=200,
                think=False,
                retries=1,
            )
            title = str(parse_json(reply.text).get("group", ""))
        except (ModelError, ValueError, AttributeError) as e:
            logger.warning("student %s could not choose: %s", self.handle, e)
            title = ""
        titles = [g["title"] for g in groups]
        return title if title in titles else titles[0]


def describe_board(board: Any) -> str:
    return "\n".join(f"{ln.describe()}" for ln in board.lines)


def summary(students: list[SimStudent]) -> dict[str, Any]:
    return {s.handle: {"calls": s.calls, "typed": len(s.typed)} for s in students}


def load_personas(scenario: dict[str, Any]) -> dict[str, str]:
    """`personas` in a scenario: {"s1": "who they are ...", ...}."""
    personas = scenario.get("personas") or {}
    if not isinstance(personas, dict):
        raise ValueError("personas is a map from s1, s2 ... to a description")
    return {str(k): str(v) for k, v in personas.items()}
