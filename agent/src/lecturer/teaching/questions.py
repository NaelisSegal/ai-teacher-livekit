"""Questions typed in the chat, and what the lecturer does about them.

Each message is shown to a fast model, which says what kind of thing it is:

    simple        answered straight away, after the sentence being spoken
    hard          "I'll come back to that": put aside, and answered at the end
    not_question  left alone
    off_topic     left alone

If the model is late or fails, the default is to leave the message alone: a missed
question is better than a lecture that stalls.

A question put aside is a promise. It is kept after the last sentence of the lesson,
on a board page of its own. (In a live class the teacher answers questions itself:
`teacher.py`. This is for a class read from its script.)

The words of a question go to the model and nowhere else. The log records that a
question came, what kind it was and how long things took, not what it said.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from ..models.chat_answers import ChatAnswerer
from ..room.chat import ChatMessage
from ..script.steps import Step, StepParser

if TYPE_CHECKING:
    from ..room.events import ClassEvents
    from ..speech.decider import VoiceDecider

logger = logging.getLogger("lecturer.questions")

MAX_PUT_ASIDE = 3
COME_BACK = "Good question. I'll come back to that at the end."
NO_ANSWER = "I said I would come back to a question, and I can't answer it properly now. Sorry."


def answer_steps(text: str, tag: str, *, board: bool, says: int, writes: int) -> list[Step]:
    """Turn a model's answer into steps the lecturer can perform.

    Only speaking and writing are taken from it. Anything else in the text (changing
    the stage, opening the mics, a poll) is dropped, so an answer cannot do more than
    answer.
    """
    parser = StepParser(strict=False)
    steps: list[Step] = []
    said = written = 0
    for line in text.splitlines():
        step = parser.feed(line)
        if step is None or step.on != "board":
            continue
        if step.verb == "say" and said < says:
            said += 1
            steps.append(step)
        elif step.verb in ("write", "note") and board and written < writes and said:
            written += 1
            step.id = f"{tag}_{written}"  # its own name, so it cannot clash with the lesson's
            step.col, step.beside, step.space = 0, "", written == 1
            steps.append(step)
    return steps if said else []


class Questions:
    def __init__(
        self, events: ClassEvents, decider: VoiceDecider, answerer: ChatAnswerer, lesson: str
    ) -> None:
        self._events = events
        self._decider = decider
        self._answerer = answerer
        self._lesson = lesson
        self._put_aside: list[str] = []
        self._recent: dict[str, float] = {}
        self._count = 0
        self._tasks: set[asyncio.Task[Any]] = set()

    def on_chat(self, message: ChatMessage) -> None:
        text = message.text.strip()
        if len(text.split()) < 2 and "?" not in text:
            return
        now = time.monotonic()
        if now - self._recent.get(text.casefold(), -1e9) < 60:
            return  # the same thing again
        self._recent[text.casefold()] = now
        task = asyncio.create_task(self._consider(text, now))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _context(self) -> str:
        said = "\n".join(f"- {line}" for line in self._events.said) or "- (nothing yet)"
        board = "\n".join(f"- {ln.describe()}" for ln in self._events.board.lines) or "- (empty)"
        return (
            f"The course and lesson: {self._lesson}\n\n"
            f"What the lecturer has said most recently:\n{said}\n\n"
            f"On the board now:\n{board}"
        )

    def _board_on_stage(self) -> bool:
        return any(p["source"] == "board" for row in self._events.stage.rows for p in row)

    async def _consider(self, text: str, asked: float) -> None:
        label = await self._answerer.label(text, self._context())
        if label is None:
            self._events.note("question", kind="no label")
            return
        self._events.note("question", kind=label.kind, label_ms=round(label.ms))
        if label.kind == "simple":
            self._count += 1
            steps = answer_steps(
                label.answer, f"q{self._count}", board=self._board_on_stage(), says=5, writes=1
            )
            if steps:
                self._decider.interject_steps(steps, source="answer", asked=asked)
        elif label.kind == "hard" and len(self._put_aside) < MAX_PUT_ASIDE:
            self._put_aside.append(text)
            self._decider.interject(COME_BACK)

    async def keep_promises(self) -> None:
        """Answer the questions that were put aside. Called when the lesson is over."""
        if not self._put_aside:
            return
        questions, self._put_aside = self._put_aside, []
        context = self._context()
        # Asked for together, while the lecturer says it is coming to them.
        answers = [asyncio.create_task(self._answerer.answer(q, context)) for q in questions]
        opening = "title: Questions\nsay: Before we finish, I said I would come back to a question."
        if len(questions) > 1:
            opening = opening.replace("to a question", "to some questions")
        from ..script.steps import parse_steps

        self._decider.interject_steps(parse_steps(opening), source="ready")
        for n, pending in enumerate(answers, 1):
            text = await pending
            steps = answer_steps(text or "", f"p{n}", board=True, says=9, writes=3)
            self._events.note("promise_kept", answered=bool(steps))
            if not steps:
                steps = [Step("say", text=NO_ANSWER)]
            self._decider.enqueue_steps(steps, source="answer")
        self._events.note(
            "model_use", tokens_in=self._answerer.tokens_in, tokens_out=self._answerer.tokens_out
        )
