"""Pictures and videos, as a script or a deck names them.

A picture or a video is either a file from lesson prep or, for a video, an address on
a site whose player can be put in the class page:

    diagram.jpg                            a file in the lesson's own folder
    other-course/diagram.jpg               a file in another course's folder
    https://www.youtube.com/watch?v=...    a YouTube video
    https://www.twitch.tv/videos/...       a Twitch video, clip or channel

An address is never sent to browsers as written. It is read here into what the site
calls the video (`VideoRef`), and each browser builds the player's address from that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

# Plain letters, digits, dots, hyphens and underscores, and a lower-case folder: the
# browser and the web app's file routes take exactly these and no others.
_FILE = r"(?:(?P<folder>[a-z0-9-]+)/)?(?P<file>\w[\w.-]*\.(?i:{ext}))"
_PICTURE = re.compile(_FILE.format(ext="svg|png|jpg|jpeg|webp|gif"), re.ASCII)
_VIDEO = re.compile(_FILE.format(ext="mp4|webm|m4v|mov"), re.ASCII)
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TWITCH_NAME = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
# In a chat message: something that might be a video's address.
_LINK = re.compile(r"https?://[^\s<>\"']+")


@dataclass(frozen=True)
class VideoRef:
    kind: str  # "file" | "youtube" | "twitch"
    id: str  # file: <folder>/<file>. youtube: the video's id. twitch: its id, slug or channel
    form: str = ""  # twitch: "video" | "clip" | "channel"
    start: float = 0.0  # where the address says to start, in seconds

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "id": self.id, "form": self.form}

    @property
    def site(self) -> str:
        return {"youtube": "YouTube", "twitch": "Twitch"}.get(self.kind, "")

    @property
    def key(self) -> str:
        """The same for two addresses of the same video."""
        return f"{self.kind}:{self.form}:{self.id}"


def picture_file(text: str, folder: str = "") -> str | None:
    """`<folder>/<file>` for a picture named in a script, or None if it is not one."""
    m = _PICTURE.fullmatch(text.strip())
    if not m or not (m["folder"] or folder):
        return None
    return f"{m['folder'] or folder}/{m['file']}"


def is_picture_file(text: str) -> bool:
    return _PICTURE.fullmatch(text.strip()) is not None


def seconds(text: str) -> float | None:
    """`90`, `1:30`, `1:02:03`, or as addresses write it, `1m30s`. None if it is none."""
    text = text.strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}", text):
        total = 0.0
        for piece in text.split(":"):
            total = total * 60 + int(piece)
        return total
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", text)
    if m and any(m.groups()):
        return float(int(m[1] or 0) * 3600 + int(m[2] or 0) * 60 + int(m[3] or 0))
    return None


def _web(address: str) -> VideoRef | None:
    try:
        url = urlparse(address)
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or not url.hostname:
        return None
    host = url.hostname.lower().removeprefix("www.").removeprefix("m.")
    path = [p for p in url.path.split("/") if p]
    query = parse_qs(url.query)
    start = seconds((query.get("t") or query.get("start") or ["0"])[0]) or 0.0

    if host == "youtu.be" and path and _YOUTUBE_ID.match(path[0]):
        return VideoRef("youtube", path[0], start=start)
    if host in ("youtube.com", "youtube-nocookie.com", "music.youtube.com"):
        if path[:1] == ["watch"] and _YOUTUBE_ID.match((query.get("v") or [""])[0]):
            return VideoRef("youtube", query["v"][0], start=start)
        if len(path) == 2 and path[0] in ("shorts", "live", "embed") and _YOUTUBE_ID.match(path[1]):
            return VideoRef("youtube", path[1], start=start)
        return None
    if host == "clips.twitch.tv" and len(path) == 1 and _TWITCH_NAME.match(path[0]):
        return VideoRef("twitch", path[0], "clip")
    if host == "twitch.tv":
        if len(path) == 2 and path[0] == "videos" and path[1].isdigit():
            return VideoRef("twitch", path[1], "video", start=start)
        if len(path) == 3 and path[1] == "clip" and _TWITCH_NAME.match(path[2]):
            return VideoRef("twitch", path[2], "clip")
        if len(path) == 1 and _TWITCH_NAME.match(path[0]):
            return VideoRef("twitch", path[0], "channel")
    return None


def video_ref(text: str, folder: str = "") -> VideoRef | None:
    """What a script or a deck means by a video, or None if it is not one."""
    text = text.strip()
    m = _VIDEO.fullmatch(text)
    if m:
        if not (m["folder"] or folder):
            return None
        return VideoRef("file", f"{m['folder'] or folder}/{m['file']}")
    return _web(text)


def is_video(text: str) -> bool:
    text = text.strip()
    return _VIDEO.fullmatch(text) is not None or _web(text) is not None


def videos_in(message: str, most: int = 3) -> list[tuple[str, VideoRef]]:
    """The video addresses in some text: each as written, and as read."""
    found = []
    for address in _LINK.findall(message):
        address = address.rstrip(".,;:!?)")
        ref = _web(address)
        if ref is not None:
            found.append((address, ref))
    return found[:most]
