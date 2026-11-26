"""The models behind the live teacher.

    the writer    writes the next few sentences of the class, as step lines, while the
                  voice is still speaking the last ones. Its lines are used as they
                  arrive, so the first sentence can be spoken before the last is written.
    the planner   looks at the whole class at checkpoints and reshapes what is left.
                  Optional: one of the things being tested is whether it helps.

Which model each one is, is a setting (`CH_WRITER`, `CH_PLANNER`: see `settings.py`).
The decision model is in `decisions.py`.

A request that is refused is treated as a failure, not sent on to another model.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .providers import Messages, Model, ModelError, Usage, parse_json

__all__ = ["Messages", "ModelError", "Plan", "Planner", "Use", "Writer"]


@dataclass
class Use:
    """What a model has cost so far in this class."""

    calls: int = 0
    tokens_in: int = 0  # not from the cache
    cache_read: int = 0
    cache_write: int = 0
    tokens_out: int = 0
    first_line_ms: list[float] = field(default_factory=list)

    def add(self, usage: Usage) -> None:
        """Count one call."""
        self.calls += 1
        self.tokens_in += usage.tokens_in
        self.tokens_out += usage.tokens_out
        self.cache_read += usage.cache_read
        self.cache_write += usage.cache_write

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "tokens_in": self.tokens_in,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "tokens_out": self.tokens_out,
        }


class Writer:
    """Writes the class a few sentences at a time."""

    def __init__(self, model: Model, *, first_line_timeout: float = 20.0) -> None:
        self._model = model
        self._first_line_timeout = first_line_timeout
        self.use = Use()

    async def lines(self, system: str, messages: Messages) -> AsyncIterator[str]:
        """The writer's next lines, each given as soon as it is complete.

        Raises ModelError if the model fails, refuses, or its first line is late.
        Stopping early (not reading to the end) is fine: the request is closed.
        """
        began = time.monotonic()
        waiting_for_first = True
        buffer = ""
        usage = Usage()
        pieces = self._model.stream(
            system,
            messages,
            usage,
            max_tokens=1500,
            # The writer answers at once, without thinking first: every second of
            # thought is a second of silence in class. (To let it think, to compare,
            # set the model's own thinking parameter in CH_WRITER_EXTRA.)
            think=False,
            effort="medium",
            # The instructions and the lesson prep are the same on every call of a
            # class, and the conversation so far only ever grows.
            cache=True,
            growing=True,
        )

        def first() -> None:
            nonlocal waiting_for_first
            if waiting_for_first:
                waiting_for_first = False
                self.use.first_line_ms.append((time.monotonic() - began) * 1000)

        try:
            async with contextlib.aclosing(pieces):  # type: ignore[type-var]
                while True:
                    try:
                        limit = self._first_line_timeout if waiting_for_first else 30.0
                        buffer += await asyncio.wait_for(pieces.__anext__(), limit)
                    except StopAsyncIteration:
                        break
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        if line.strip():
                            first()
                            yield line.strip()
                if buffer.strip():
                    first()
                    yield buffer.strip()
        except TimeoutError:
            raise ModelError("the writer was too slow") from None
        except ModelError as e:
            raise ModelError(f"the writer failed: {e}") from e
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except Exception as e:  # nothing the writer does may end the class
            raise ModelError(f"the writer failed: {type(e).__name__}") from e
        finally:
            # Counted however the turn ended: to the end, at a pause, or cut off.
            if usage.any:
                self.use.add(usage)


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "minutes": {"type": "number"},
                    "instruction": {"type": "string"},
                },
                "required": ["id", "minutes", "instruction"],
                "additionalProperties": False,
            },
        },
        "promises": {"type": "array", "items": {"type": "string"}},
        "changed": {"type": "boolean"},
        "why": {"type": "string"},
    },
    "required": ["parts", "promises", "changed", "why"],
    "additionalProperties": False,
}


@dataclass
class Plan:
    parts: list[dict[str, Any]]
    promises: list[str]
    changed: bool
    why: str

    def as_text(self) -> str:
        lines = []
        for part in self.parts:
            line = f"- part {part['id']}: {part['minutes']:g} min"
            if part["instruction"]:
                line += f". {part['instruction']}"
            lines.append(line)
        lines.extend(f"- put aside: {p}" for p in self.promises)
        return "\n".join(lines)


class Planner:
    """Reshapes the rest of the class at checkpoints."""

    def __init__(self, model: Model) -> None:
        self._model = model
        self.use = Use()

    async def plan(self, system: str, situation: str, *, timeout: float = 60.0) -> Plan:
        try:
            reply = await asyncio.wait_for(
                self._model.complete(
                    system,
                    [{"role": "user", "content": situation}],
                    max_tokens=6000,
                    schema=PLAN_SCHEMA,
                    # It is not heard by anyone and is given time to think.
                    effort="medium",
                    cache=True,
                    retries=1,
                ),
                timeout,
            )
        except TimeoutError:
            raise ModelError("the planner was too slow") from None
        except ModelError as e:
            raise ModelError(f"the planner failed: {e}") from e
        self.use.add(reply.usage)
        try:
            data = parse_json(reply.text)
            return Plan(data["parts"], data["promises"], data["changed"], data["why"])
        except (ValueError, KeyError, TypeError) as e:
            raise ModelError("the planner's answer could not be read") from e
