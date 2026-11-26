"""Reading text that people send, without trusting its size.

Anyone in the room may send on the chat and reaction topics, and a stream can be
made very long. So incoming text is read a piece at a time and dropped once it is
longer than it has any reason to be. A reader that is not read to its end must be
closed, or it stays subscribed for the life of the process.
"""

from __future__ import annotations

import contextlib

from livekit import rtc


async def read_capped(reader: rtc.TextStreamReader, max_chars: int) -> str | None:
    """The stream's text, or None if it is longer than `max_chars` or fails."""
    parts: list[str] = []
    length = 0
    try:
        async for chunk in reader:
            length += len(chunk)
            if length > max_chars:
                refuse(reader)
                return None
            parts.append(chunk)
    except Exception:
        refuse(reader)
        return None
    return "".join(parts)


def refuse(reader: rtc.TextStreamReader) -> None:
    """Drop a stream we are not going to read."""
    with contextlib.suppress(Exception):
        reader.close()
