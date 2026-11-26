"""Class chat, on its own topic.

Students type on `ch.chat`. It is not LiveKit's default `lk.chat` topic, which makes
an agent stop and answer every message. Messages go to everyone in the room, and
arrive here for the lecturer to consider.

What students type is never written to the event log.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from livekit import rtc

from .streams import read_capped, refuse

logger = logging.getLogger("lecturer.chat")

CHAT_TOPIC = "ch.chat"
MAX_CHARS = 500
RATE_MAX, RATE_WINDOW_SECONDS = 5, 10  # messages per student the lecturer will read


@dataclass
class ChatMessage:
    identity: str
    name: str
    text: str
    at: float = field(default_factory=time.time)


class Chat:
    def __init__(self, room: rtc.Room, on_message: Callable[[ChatMessage], None]) -> None:
        self._room = room
        self._on_message = on_message
        self._tasks: set[asyncio.Task[None]] = set()
        self._recent: dict[str, list[float]] = {}

    def register(self) -> None:
        self._room.register_text_stream_handler(CHAT_TOPIC, self._on_stream)

    def _on_stream(self, reader: rtc.TextStreamReader, identity: str) -> None:
        task = asyncio.create_task(self._read(reader, identity))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read(self, reader: rtc.TextStreamReader, identity: str) -> None:
        # Anyone sending too fast is not read at all.
        now = time.monotonic()
        recent = [t for t in self._recent.get(identity, []) if now - t < RATE_WINDOW_SECONDS]
        if len(recent) >= RATE_MAX:
            refuse(reader)
            return
        self._recent[identity] = [*recent, now]
        text = await read_capped(reader, MAX_CHARS * 4)
        if text is None:
            return
        text = text.strip()[:MAX_CHARS]
        if not text:
            return
        who = self._room.remote_participants.get(identity)
        self._on_message(ChatMessage(identity, (who.name if who else "") or identity, text))
