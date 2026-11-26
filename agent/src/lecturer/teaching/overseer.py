"""The overseer: another model that keeps an eye on the groups while a class is split.

Each group's lecturer reports how it is going (what it has said lately, how long it
has run, how many students it has: never what students said). Every so often the
overseer is shown the reports and may end a group early and bring its students back:
one that has done what it was for, has nobody in it, or has wandered off what it was
for. It does not teach and it does not speak. It is not shown a private group (one
student taken aside), which is left to its own lecturer and the helper in it.

It uses the planner's model, or the writer's if there is no planner. With neither,
groups run for their time and nobody but the helpers is watching them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from ..models.providers import Model, ModelError, parse_json
from ..room.events import ClassEvents
from ..room.groups import Groups

logger = logging.getLogger("lecturer.overseer")

SYSTEM = """\
A live online class has split into groups. Each group has its own lecturer and a human
helper. You are keeping an eye on the groups from outside. You are shown, for each
group, what it is for, how long it has run, how many students are in it, and the last
things its lecturer said.

Decide whether any group should be ended now and its students brought back to the main
class. End a group only for a clear reason: it has done what it was for, it has no
students, or it has plainly left what it was for. A group that is working is left
alone, and that is the usual answer: give an empty list.

Reply with the titles of the groups to end, exactly as given, and one sentence of why.
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "end": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string"},
    },
    "required": ["end", "why"],
    "additionalProperties": False,
}


def reports_text(reports: list[dict[str, Any]]) -> str:
    """The groups' reports in words, for a model."""
    lines = []
    for r in reports:
        if r.get("private"):
            lines.append(f'- "{r["title"]}": one student taken aside. Private: not reported on.')
            continue
        said = " ".join(r.get("last_said") or []) or "(nothing yet)"
        state = "ended" if r.get("ended") else f"{r.get('minutes_in', 0):g} min in"
        lines.append(
            f'- "{r["title"]}" ({state}, {r.get("students", "?")} students). '
            f'For: {r["for"]}. Its lecturer last said: "{said}"'
        )
    return "\n".join(lines)


class Overseer:
    def __init__(
        self, groups: Groups, model: Model, events: ClassEvents, *, every: float = 45.0
    ) -> None:
        self._groups = groups
        self._model = model
        self._events = events
        self._every = every

    async def look(self) -> list[str]:
        """Look at the groups once. The titles of the groups it ended."""
        reports = [r for r in self._groups.reports() if not r["private"] and not r["ended"]]
        if not reports:
            return []
        try:
            reply = await asyncio.wait_for(
                self._model.complete(
                    SYSTEM,
                    [{"role": "user", "content": reports_text(reports)}],
                    max_tokens=1000,
                    schema=SCHEMA,
                    effort="low",
                ),
                30,
            )
            answer = parse_json(reply.text)
            wanted = [str(t) for t in answer["end"]]
        except (TimeoutError, ModelError, ValueError, KeyError, TypeError) as e:
            logger.warning("the overseer could not look at the groups: %s", e)
            return []
        titles = {r["title"].casefold(): r["title"] for r in reports}
        ended = [titles[t.casefold()] for t in wanted if t.casefold() in titles]
        for title in ended:
            self._events.note("overseer_ended_group", why=json.dumps(answer.get("why", ""))[:300])
            await self._groups.close(title)
        return ended

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._every)
            if self._groups.open_now:
                await self.look()
