"""Hearing what people say aloud.

A student may speak only when they have the floor or the mics are open (`floor.py`
grants that), and a helper or guest at any time. Whatever they say is cut into
stretches at their pauses, and each stretch is written down by a model that takes
audio in, then handed to the teacher like a chat message, marked as said aloud.

Which model is a setting, like the others: CH_HEARING=provider:model, for a model
that takes audio (on OpenRouter, for example, CH_HEARING=openrouter:google/gemini-3.8-flash).
Without it nothing is written down: the lecturer still stops while people talk, and
is told that someone spoke and it could not hear what.

What is said is treated like what is typed: it goes to the models, and it is never
written to the class log.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from livekit import rtc

from ..models.providers import Model, ModelError

logger = logging.getLogger("lecturer.hearing")

RATE = 16000
VOICED = 0.012  # a frame louder than this (RMS, of full scale) is someone speaking
END_SILENCE = 0.9  # seconds of quiet that end a stretch of speech
MIN_SPEECH = 0.35  # seconds of voiced sound below which a stretch is just noise
MAX_STRETCH = 25.0  # a longer stretch is cut here and written down in parts
PRE_ROLL = 0.3  # seconds kept from before the speech began, so its start is not lost

WRITE_DOWN = (
    "This is a recording of one person speaking in a live online class. Write down "
    "exactly what they say, word for word, as plain text, with no comments. If there "
    "is no speech, only noise, reply with exactly: (nothing)"
)


def wav_bytes(pcm: np.ndarray) -> bytes:
    """16-bit mono WAV of some samples (floats in -1..1)."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())
    return out.getvalue()


@dataclass
class Stretch:
    """Speech being collected from one person."""

    frames: list[np.ndarray] = field(default_factory=list)
    voiced: float = 0.0  # seconds of it that were voiced
    length: float = 0.0
    quiet: float = 0.0  # seconds of quiet since the last voiced frame


async def write_down(model: Model, pcm: np.ndarray) -> str:
    """What was said in a clip, or "" if nothing was. Raises ModelError."""
    clip = base64.b64encode(wav_bytes(pcm)).decode()
    reply = await model.complete(
        WRITE_DOWN,
        [
            {
                "role": "user",
                "content": [
                    {"type": "input_audio", "input_audio": {"data": clip, "format": "wav"}}
                ],
            }
        ],
        max_tokens=600,
        retries=1,
    )
    text = " ".join(reply.text.split())
    return "" if text.strip("().").lower() in ("", "nothing") else text


class Hearing:
    """Listens to everyone's microphone, and says what they said."""

    def __init__(
        self,
        room: rtc.Room,
        model: Model | None,
        heard: Callable[[str, str], None],
        *,
        most: int | None = None,
    ) -> None:
        """`heard(identity, text)`: something was said and written down. `most`: the
        most clips one class may send to the model (CH_HEARING_LIMIT); after that,
        nothing more is written down."""
        self._room = room
        self.model = model
        self._heard = heard
        self._most = most
        self.calls = 0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._order: dict[str, asyncio.Lock] = {}  # one person's words in the order said

    def register(self) -> None:
        self._room.on("track_subscribed", self._on_track)
        # Tracks subscribed before we were listening.
        for person in self._room.remote_participants.values():
            for pub in person.track_publications.values():
                if pub.track is not None and pub.kind == rtc.TrackKind.KIND_AUDIO:
                    self._on_track(pub.track, pub, person)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_track(
        self, track: rtc.Track, pub: rtc.RemoteTrackPublication, person: rtc.RemoteParticipant
    ) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        if pub.source != rtc.TrackSource.SOURCE_MICROPHONE:
            return  # the sound of a shared screen is not someone speaking
        if person.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            return
        self._spawn(self._listen(track, person.identity))

    async def _listen(self, track: rtc.Track, identity: str) -> None:
        stretch = Stretch()
        before: list[np.ndarray] = []  # the last moments of quiet, for the pre-roll
        async for ev in rtc.AudioStream(track, sample_rate=RATE, num_channels=1):
            pcm = np.frombuffer(ev.frame.data, dtype=np.int16).astype(np.float32) / 32768
            seconds = len(pcm) / RATE
            loud = float(np.sqrt(np.mean(pcm * pcm))) > VOICED if len(pcm) else False
            if not stretch.frames:
                if not loud:
                    before = [*before, pcm][-max(1, int(PRE_ROLL / max(seconds, 1e-3))) :]
                    continue
                stretch.frames, before = [*before, pcm], []
                stretch.length = seconds * len(stretch.frames)
                stretch.voiced = seconds
                continue
            stretch.frames.append(pcm)
            stretch.length += seconds
            if loud:
                stretch.voiced += seconds
                stretch.quiet = 0.0
            else:
                stretch.quiet += seconds
            if stretch.quiet >= END_SILENCE or stretch.length >= MAX_STRETCH:
                self._finish(identity, stretch)
                stretch = Stretch()
        if stretch.frames:
            self._finish(identity, stretch)

    def _finish(self, identity: str, stretch: Stretch) -> None:
        if stretch.voiced < MIN_SPEECH:
            return
        if self.model is None or (self._most is not None and self.calls >= self._most):
            return
        self.calls += 1
        pcm = np.concatenate(stretch.frames)
        lock = self._order.setdefault(identity, asyncio.Lock())
        written = asyncio.create_task(self._write(pcm))
        self._tasks.add(written)
        written.add_done_callback(self._tasks.discard)

        async def deliver() -> None:
            async with lock:  # in the order said, even if a later one is written sooner
                with contextlib.suppress(asyncio.CancelledError):
                    text = await written
                    if text:
                        self._heard(identity, text)

        self._spawn(deliver())

    async def _write(self, pcm: np.ndarray) -> str:
        assert self.model is not None
        try:
            return await write_down(self.model, pcm)
        except ModelError as e:
            logger.warning("could not write down what was said: %s", e)
            return ""

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
