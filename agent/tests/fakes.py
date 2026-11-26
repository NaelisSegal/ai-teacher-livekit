"""Pretend versions of LiveKit and the voice, shared by the tests.

They do only what the code under test needs: a room holds people and records what
is sent and which handlers are registered; a voice "says" a sentence by waiting a
moment. Nothing here talks to a network or makes a sound.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

from lecturer.speech.voice import Played

STUDENT = "student"
HELPER = "helper"


@dataclass
class PretendPerson:
    """Someone in a room: a student, or staff. `person` is set for a second tab of
    someone already here (who they are); it defaults to their own identity."""

    identity: str
    role: str = STUDENT
    name: str = ""
    kind: int = 0  # not an agent
    person: str = ""

    @property
    def attributes(self) -> dict[str, str]:
        return {"ch.role": self.role, "ch.person": self.person or self.identity}


class PretendLocal:
    """The lecturer's own side of a room."""

    identity = "lecturer"

    def __init__(self) -> None:
        self.rpc: dict[str, Any] = {}
        self.sent: list[tuple[str, dict[str, Any]]] = []  # (text, how it was sent)

    async def send_text(self, text: str, **how: Any) -> None:
        self.sent.append((text, how))

    def register_rpc_method(self, name: str, handler: Any) -> None:
        self.rpc[name] = handler

    def events(self) -> list[dict[str, Any]]:
        """Every class event sent, in order."""
        out = []
        for text, _ in self.sent:
            message = json.loads(text)
            if message.get("type") == "events":
                out.extend(message["events"])
        return out


class PretendRoom:
    name = "class-1"

    def __init__(self, *people: PretendPerson) -> None:
        self.local_participant = PretendLocal()
        self.remote_participants: dict[str, Any] = {p.identity: p for p in people}
        self.handlers: dict[str, Any] = {}

    def on(self, event: str, handler: Any) -> None:
        self.handlers[event] = handler


@dataclass
class PretendClip:
    text: str
    ready: bool = True
    duration_ms: float = 30
    first_audio_ms: float = 1
    error: Exception | None = None

    def cancel(self) -> None:
        return None


class PretendVoice:
    """Each sentence takes `seconds`. Sentences in `broken` give no audio, as a
    failing text-to-speech service would. stop() cuts the sentence being said."""

    def __init__(self, seconds: float = 0.03, broken: set[str] | None = None) -> None:
        self.seconds = seconds
        self.broken = broken or set()
        self.prepared: list[str] = []
        self._stop = asyncio.Event()

    def prepare(self, text: str) -> PretendClip:
        self.prepared.append(text)
        return PretendClip(text, duration_ms=self.seconds * 1000)

    async def play(self, clip: PretendClip, on_start: Any) -> Played:
        self._stop.clear()
        if clip.text in self.broken:
            return Played(False, 0, started=False, failed=True)
        await on_start()
        try:
            await asyncio.wait_for(self._stop.wait(), self.seconds)
        except TimeoutError:
            return Played(True, self.seconds * 1000)
        return Played(False, self.seconds * 300)

    def stop(self) -> None:
        self._stop.set()


async def until(test: Any, seconds: float = 5) -> None:
    """Wait until `test()` is true, or fail."""

    async def poll() -> None:
        while not test():
            await asyncio.sleep(0.002)

    await asyncio.wait_for(poll(), seconds)
