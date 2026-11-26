"""The lecturer's voice: one audio track, played one clip at a time.

A clip is one sentence. It can be prepared ahead of time, so the next sentence is
ready when the current one ends, and it can be stopped at once.

With no voice set (CH_TTS), the lecturer has none: `SilentVoice` shows each sentence
as a caption for about as long as it would take to say, so that the class keeps its
pace.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from livekit import rtc
from livekit.agents import tts

logger = logging.getLogger("lecturer.voice")


@dataclass
class Clip:
    text: str
    requested_at: float = field(default_factory=time.perf_counter)
    first_audio_at: float | None = None
    duration_ms: float = 0.0
    error: BaseException | None = None
    _frames: asyncio.Queue[rtc.AudioFrame | None] = field(default_factory=asyncio.Queue)
    _done: asyncio.Event = field(default_factory=asyncio.Event)
    _task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        """True once all of the clip's audio has been made."""
        return self._done.is_set()

    @property
    def first_audio_ms(self) -> float | None:
        """Time from asking for speech to the first audio arriving."""
        if self.first_audio_at is None:
            return None
        return (self.first_audio_at - self.requested_at) * 1000

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()


@dataclass
class Played:
    completed: bool
    played_ms: float
    started: bool = True  # False if not one frame of audio was played
    failed: bool = False  # text-to-speech gave an error


class Voice:
    def __init__(self, room: rtc.Room, engine: tts.TTS) -> None:
        self._room = room
        self._tts = engine
        self._source = rtc.AudioSource(engine.sample_rate, engine.num_channels, queue_size_ms=400)
        self._stop = asyncio.Event()
        self._playing = False

    async def start(self) -> None:
        track = rtc.LocalAudioTrack.create_audio_track("lecturer-voice", self._source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self._room.local_participant.publish_track(track, options)

    def prepare(self, text: str) -> Clip:
        """Start making the audio for a sentence. Returns at once."""
        clip = Clip(text)
        clip._task = asyncio.create_task(self._synthesize(clip))
        return clip

    async def _synthesize(self, clip: Clip) -> None:
        try:
            async with contextlib.aclosing(self._tts.synthesize(clip.text)) as stream:
                async for audio in stream:
                    frame = audio.frame
                    if clip.first_audio_at is None:
                        clip.first_audio_at = time.perf_counter()
                    clip.duration_ms += frame.duration * 1000
                    clip._frames.put_nowait(frame)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            clip.error = e
            logger.exception(
                "text-to-speech failed for a sentence of %d characters", len(clip.text)
            )
        finally:
            clip._done.set()
            clip._frames.put_nowait(None)

    async def play(
        self, clip: Clip, on_start: Callable[[], Awaitable[None]] | None = None
    ) -> Played:
        """Play a clip to the end, or until stop() is called."""
        self._stop.clear()
        self._playing = True
        started: float | None = None
        try:
            while not self._stop.is_set():
                frame = await self._next_frame(clip)
                if frame is None:
                    break
                if started is None:
                    started = time.perf_counter()
                    if on_start:
                        await on_start()
                await self._source.capture_frame(frame)
            if started is None:
                # Stopped before any audio, or text-to-speech gave nothing. The caller
                # decides what to do: on_start has not run, so nothing was shown.
                return Played(False, 0, started=False, failed=not self._stop.is_set())
            if not self._stop.is_set():
                waiter = asyncio.ensure_future(self._source.wait_for_playout())
                stopper = asyncio.ensure_future(self._stop.wait())
                await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
                stopper.cancel()
            elapsed = (time.perf_counter() - started) * 1000
            if self._stop.is_set():
                self._source.clear_queue()
                clip.cancel()
                return Played(completed=False, played_ms=min(elapsed, clip.duration_ms))
            # Audio that broke off partway was not heard in full.
            failed = clip.error is not None
            return Played(completed=not failed, played_ms=clip.duration_ms, failed=failed)
        finally:
            self._playing = False

    async def _next_frame(self, clip: Clip) -> rtc.AudioFrame | None:
        """The clip's next frame, or None at its end or when stop() is called."""
        if not clip._frames.empty():
            return clip._frames.get_nowait()
        getter = asyncio.ensure_future(clip._frames.get())
        stopper = asyncio.ensure_future(self._stop.wait())
        await asyncio.wait({getter, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        if getter.done():
            return getter.result()
        getter.cancel()
        return None

    def stop(self) -> None:
        """Stop the voice now. The clip being played is cut off."""
        if self._playing:
            self._stop.set()
            self._source.clear_queue()


WORDS_PER_SECOND = 2.6  # how long a sentence takes to say, when there is no voice


class SilentVoice:
    """No voice: each sentence takes the time it would take to say, and is heard by
    nobody. The captions and the board carry the class."""

    def __init__(self) -> None:
        self._stop = asyncio.Event()

    async def start(self) -> None:
        return None

    def prepare(self, text: str) -> Clip:
        clip = Clip(text, duration_ms=len(text.split()) / WORDS_PER_SECOND * 1000 + 300)
        clip.first_audio_at = clip.requested_at
        clip._done.set()
        return clip

    async def play(
        self, clip: Clip, on_start: Callable[[], Awaitable[None]] | None = None
    ) -> Played:
        self._stop.clear()
        if on_start:
            await on_start()
        began = time.perf_counter()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), clip.duration_ms / 1000)
        if self._stop.is_set():
            return Played(False, (time.perf_counter() - began) * 1000)
        return Played(True, clip.duration_ms)

    def stop(self) -> None:
        self._stop.set()
