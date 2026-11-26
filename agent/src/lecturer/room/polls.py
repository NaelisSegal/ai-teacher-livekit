"""Polls: a question from the lecturer with a few options, shown in the chat.

A poll is the one thing the lecturer puts in the chat: it does not otherwise write
there.

Students vote by RPC; each has one vote and may change it. Everyone sees the totals,
with no names. The log keeps the question, the options and the totals.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from livekit import rtc

from .floor import person_of

if TYPE_CHECKING:
    from .events import ClassEvents

logger = logging.getLogger("lecturer.polls")

KEEP = 3  # how many recent polls stay in live state


@dataclass
class PollState:
    """Live state for the snapshot. The browser has the same rules in store.ts."""

    polls: list[dict[str, Any]] = field(default_factory=list)

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(p, options=list(p["options"]), counts=list(p["counts"])) for p in self.polls]

    def apply(self, ev: dict[str, Any]) -> None:
        t = ev["type"]
        if t == "poll.open":
            for poll in self.polls:
                poll["open"] = False
            self.polls.append(
                {
                    "id": ev["id"],
                    "question": ev["question"],
                    "options": list(ev["options"]),
                    "counts": [0] * len(ev["options"]),
                    "open": True,
                }
            )
            self.polls = self.polls[-KEEP:]
        elif t in ("poll.results", "poll.close"):
            for poll in self.polls:
                if poll["id"] == ev["id"]:
                    if "counts" in ev:
                        poll["counts"] = list(ev["counts"])
                    if t == "poll.close":
                        poll["open"] = False


class Polls:
    def __init__(self, room: rtc.Room, events: ClassEvents) -> None:
        self._room = room
        self._events = events
        self._id = ""
        self._options = 0
        self._votes: dict[str, int] = {}  # person -> option, for the open poll
        self._recent: dict[str, list[float]] = {}
        self._dirty = False
        self._sender: asyncio.Task[None] | None = None
        self.on_vote: Callable[[], None] | None = None  # told after each vote is counted

    def register(self) -> None:
        self._room.local_participant.register_rpc_method("ch.vote", self._on_vote)

    def voters(self) -> set[str]:
        """Who has voted in the open poll, as people (`floor.person_of`)."""
        return set(self._votes)

    def _counts(self) -> list[int]:
        counts = [0] * self._options
        for option in self._votes.values():
            counts[option] += 1
        return counts

    async def open(self, question: str, options: list[str]) -> None:
        self._id, self._options, self._votes = uuid.uuid4().hex[:8], len(options), {}
        await self._events.emit(
            {"type": "poll.open", "id": self._id, "question": question, "options": options}
        )

    async def close(self) -> None:
        if self._id:
            closing, self._id = self._id, ""
            await self._events.emit({"type": "poll.close", "id": closing, "counts": self._counts()})

    async def _on_vote(self, data: rtc.RpcInvocationData) -> str:
        # One vote for each person, however many tabs they have the class open in.
        caller = self._room.remote_participants.get(data.caller_identity)
        who, now = person_of(caller, data.caller_identity), time.monotonic()
        recent = [t for t in self._recent.get(who, []) if now - t < 5]
        if len(recent) >= 8:
            return "slow down"
        self._recent[who] = [*recent, now]
        try:
            ask = json.loads(data.payload)
            poll, option = ask["poll"], int(ask["option"])
        except (ValueError, KeyError, TypeError):
            raise rtc.RpcError(400, "bad request") from None
        if poll != self._id or not self._id:
            return "closed"
        if not 0 <= option < self._options:
            raise rtc.RpcError(400, "no such option")
        if self._votes.get(who) != option:
            self._votes[who] = option
            self._dirty = True
            if self._sender is None or self._sender.done():
                self._sender = asyncio.create_task(self._send_soon())
        return "ok"

    async def _send_soon(self) -> None:
        """Send the totals shortly after a vote, so a burst of votes is one message."""
        while self._dirty:
            await asyncio.sleep(0.3)
            self._dirty = False
            if self._id:
                await self._events.emit(
                    {"type": "poll.results", "id": self._id, "counts": self._counts()}
                )
                if self.on_vote is not None:
                    self.on_vote()
