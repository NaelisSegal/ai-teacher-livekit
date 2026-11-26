"""A text-to-speech stand-in that uses the Mac's built-in `say` command.

It costs nothing and needs no account, so the rest of the lecturer can be built and
timed before a real voice is chosen. It is not the voice for real classes. Any
`livekit.agents.tts.TTS` can replace it: see `make_tts` beside this file.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
import wave
from pathlib import Path

from livekit.agents import APIConnectOptions, tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

SAMPLE_RATE = 24000


class MacSayTTS(tts.TTS):
    def __init__(self, *, voice: str = "Samantha", words_per_minute: int = 165) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=SAMPLE_RATE,
            num_channels=1,
        )
        self.voice = voice
        self.words_per_minute = words_per_minute

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return _SayStream(tts=self, input_text=text, conn_options=conn_options)


class _SayStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        voice: MacSayTTS = self._tts  # type: ignore[assignment]
        with tempfile.TemporaryDirectory(prefix="ch-say-") as tmp:
            out = Path(tmp) / "out.wav"
            proc = await asyncio.create_subprocess_exec(
                "say",
                "-v", voice.voice,
                "-r", str(voice.words_per_minute),
                "-o", str(out),
                "--file-format=WAVE",
                f"--data-format=LEI16@{SAMPLE_RATE}",
                "--",
                self._input_text,
                stderr=asyncio.subprocess.PIPE,
            )  # fmt: skip
            _, err = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"say failed: {err.decode(errors='replace').strip()}")
            with wave.open(str(out), "rb") as w:
                pcm = w.readframes(w.getnframes())
        output_emitter.initialize(
            request_id=uuid.uuid4().hex,
            sample_rate=SAMPLE_RATE,
            num_channels=1,
            mime_type="audio/pcm",
        )
        output_emitter.push(pcm)
        output_emitter.flush()
