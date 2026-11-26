"""Videos that students share in the chat.

A student may paste the address of a YouTube or Twitch video. The lecturer is told
what it is, in words, and decides whether it fits the lesson and whether there is
time: nothing here judges that. Only the title and who posted it are looked up, and
only for YouTube, which gives them to anyone who asks. Nothing about the student is
sent: the request is the video's address and no more.
"""

from __future__ import annotations

import logging

import aiohttp

from ..script.media import VideoRef

logger = logging.getLogger("lecturer.links")

OEMBED = "https://www.youtube.com/oembed"


def _plain(text: object, most: int = 120) -> str:
    """A title as one short line. It is someone else's text, shown to the lecturer."""
    return " ".join(str(text or "").split())[:most].replace('"', "'")


async def describe_video(ref: VideoRef, *, timeout: float = 3.0) -> str:
    """A shared video in words: where it is from and, if the site will say, its title."""
    what = {"clip": "clip", "channel": "live channel"}.get(ref.form, "video")
    title = by = ""
    if ref.kind == "youtube":
        params = {"url": f"https://www.youtube.com/watch?v={ref.id}", "format": "json"}
        try:
            limit = aiohttp.ClientTimeout(total=timeout)
            async with (
                aiohttp.ClientSession(timeout=limit) as http,
                http.get(OEMBED, params=params) as response,
            ):
                if response.status == 200:
                    found = await response.json()
                    title, by = _plain(found.get("title")), _plain(found.get("author_name"), 60)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            logger.info("could not look up a shared video")
    out = f"a {ref.site} {what}"
    if title:
        out += f': "{title}"' + (f", from {by}" if by else "")
    elif ref.form == "channel":
        out += f": {_plain(ref.id, 60)}"
    else:
        out += " (its title could not be found)"
    return out
