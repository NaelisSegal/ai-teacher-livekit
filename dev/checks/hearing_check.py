"""Is what people say aloud heard and written down? A stand-in helper (who may
speak at any time) joins a class and plays a recording into its microphone; the
class log should then show that speech was written down.

    cd agent && PYTHONPATH=../dev/checks uv run python ../dev/checks/hearing_check.py <speech.wav>

The recording: a 16 kHz, 16-bit mono WAV of someone speaking a sentence or two. Record
one yourself, or make one with a text-to-speech tool, for example:
    say -o speech.wav --data-format=LEI16@16000 "Is the radius six or three?"   (Mac)
    espeak-ng -w speech.wav "Is the radius six or three?"   (Linux; then convert it to
                                                            16 kHz with sox or ffmpeg)
Needs the stack running, and CH_HEARING and CH_WRITER set (see settings.py): only a
class taught live listens, so the check starts one, of the example lesson.
"""

from __future__ import annotations

import asyncio
import sys
import time
import wave

import numpy as np
from livekit import rtc
from student_bot import URL, ensure_lecturer, newest_log, token

RATE = 16000


async def main() -> int:
    path = sys.argv[1]
    room_name = f"live-hearing-check-{int(time.time())}"
    await ensure_lecturer(room_name, {"lesson": "example/cone", "live": True, "start_delay": 1})
    room = rtc.Room()
    await room.connect(URL, token(room_name, "bot-helper", "Helper", "helper"))
    source = rtc.AudioSource(RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    await room.local_participant.publish_track(track, options)
    await asyncio.sleep(6)  # the lecturer arrives and subscribes
    with wave.open(path, "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    step = RATE // 100  # 10 ms frames, in real time
    silence = np.zeros(step * 150, dtype=np.int16)
    for chunk in (pcm, silence):
        for i in range(0, len(chunk) - step + 1, step):
            frame = rtc.AudioFrame(chunk[i : i + step].tobytes(), RATE, 1, step)
            await source.capture_frame(frame)
    heard = []
    for _ in range(40):
        heard = [e for e in newest_log(room_name) if e.get("type") == "note.speech"]
        if heard:
            break
        await asyncio.sleep(0.5)
    await room.disconnect()
    print({"spoken_seconds": round(len(pcm) / RATE, 1), "heard": heard})
    ok = bool(heard) and heard[0].get("chars", 0) > 10
    print("hearing check:", "passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
