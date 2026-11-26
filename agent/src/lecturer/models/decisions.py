"""The decision model: fast yes-or-no judgements about what is happening in class.

It never decides what the teacher says. It answers small questions quickly, so that
plain code knows when the teacher needs to be asked, and so that the teacher is told
how the class is doing: "is this message for the lecturer?", "is this an answer to
the task?", "is the answer right?".

Every question is asked the way decision models are trained to be asked: as a
forecasting question that has already resolved, answered with the probability that
it resolved YES. Which model answers is a setting (`CH_DECISIONS`: see `settings.py`).
A decision model on OpenRouter is what this was made for; an ordinary small model
can stand in for one. With the setting off there is no decision model, and the
teacher is shown every chat message and judges for itself.

If the model is late or fails, the answer is None and the caller uses its default,
which is always to let the teacher see the thing and decide.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from ..settings import limit, model_for
from .providers import Model, ModelError, parse_json

logger = logging.getLogger("lecturer.decisions")

FRAME = (
    "The forecasting question below has already resolved. "
    "What is the probability that it resolved YES?"
)
SCHEMA = {
    "type": "object",
    "properties": {
        "probability": {
            "type": "number",
            "description": "Probability that the question resolved YES, from 0 to 1",
        }
    },
    "required": ["probability"],
    "additionalProperties": False,
}


class Decisions(Protocol):
    async def ask_many(self, questions: list[str]) -> list[float | None]:
        """The probability of YES for each question, asked at the same time."""
        ...


def _probability(text: str) -> float | None:
    try:
        p = float(parse_json(text)["probability"])
    except (ValueError, KeyError, TypeError):
        return None
    return p if 0 <= p <= 1 else None


class ModelDecisions:
    """A model answering decision questions."""

    def __init__(self, model: Model, *, timeout: float = 4.0, most: int | None = None) -> None:
        self._model = model
        self._timeout = timeout
        self._left = most  # calls this class may still make. None is no limit
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    async def _ask(self, question: str) -> float | None:
        if self._left is not None:
            if self._left <= 0:
                return None
            self._left -= 1
        try:
            reply = await asyncio.wait_for(
                self._model.complete(
                    "",
                    [{"role": "user", "content": f"{FRAME}\n\n{question}"}],
                    max_tokens=100,
                    schema=SCHEMA,
                    # Decision models answer without reasoning first.
                    think=False,
                    effort="low",
                ),
                self._timeout,
            )
        except TimeoutError:
            logger.warning("a decision was late")
            return None
        except ModelError as e:
            logger.warning("a decision failed: %s", e)
            return None
        self.calls += 1
        self.tokens_in += reply.usage.tokens_in
        self.tokens_out += reply.usage.tokens_out
        return _probability(reply.text)

    async def ask_many(self, questions: list[str]) -> list[float | None]:
        return list(await asyncio.gather(*(self._ask(q) for q in questions)))


def make_decisions() -> Decisions | None:
    """The decision model this process is set up for, or None if there is none."""
    model = model_for("decisions")
    return ModelDecisions(model, most=limit("decisions")) if model is not None else None


# -- the questions -----------------------------------------------------------------


def scene(lesson: str, said: list[str], message: str, doing: str = "") -> str:
    """What every question about a chat message starts with."""
    recent = " ".join(said[-2:]) or "(the class has only just begun)"
    lines = [
        f'During a live online class on "{lesson}", a student typed this in the class chat:',
        f'"{message}"',
        f'The lecturer had just said: "{recent}"',
    ]
    if doing:
        lines.append(doing)
    return "\n".join(lines)


def for_teacher(scene_text: str) -> str:
    return (
        f"{scene_text}\n"
        "Is the message something the lecturer should respond to: a question, a request "
        "(to explain again, go slower, go faster, make it harder), a correction, or a "
        "problem with the class?\n"
        "Resolves YES if a good teacher would respond to it in some way. Resolves NO if "
        "it is an answer to a task the class was set, a greeting, thanks, a reaction, a "
        "joke, or a message to other students."
    )


def urgent(scene_text: str) -> str:
    return (
        f"{scene_text}\n"
        "Is this something the lecturer must deal with at once, before saying anything "
        "more of the lesson: a problem with the sound, the board or the stream, a mistake "
        "the lecturer has just made, or someone in distress?\n"
        "Resolves NO for ordinary questions and requests, which can wait a few sentences."
    )


def is_answer(scene_text: str) -> str:
    return (
        f"{scene_text}\n"
        "Is the message the student's answer to that task or poll?\n"
        "Resolves YES if it gives an answer, even a wrong one or a guess. A wrong answer "
        "can look like the question's own numbers or letters given back, and still "
        "counts. Resolves NO if it asks the lecturer something, or is about anything else."
    )


def is_right(scene_text: str, expected: str) -> str:
    return (
        f"{scene_text}\n"
        f"The right answer is: {expected}\n"
        "Has the student reached the right answer?\n"
        "Resolves YES if their message gives the right answer, whatever its spelling, "
        "spacing or wording, with or without working. Resolves NO if it gives a different "
        "answer, or only working or a step on the way, even a correct one."
    )
