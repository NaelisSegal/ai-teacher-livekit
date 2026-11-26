"""The shared screen: a real browser beside the lecturer, sent to the class as video.

It is for the web and for anything that is not a board or a slide. The lecturer
steers it with steps: `open:` and `prepare:` make tabs, `tab:` switches between
them, `scroll:` moves the page. The browser is a headless Chrome with its own
throwaway profile, started the first time it is needed.

Only the page itself is sent as video. The tab strip and the address are sent as
facts (`state()`), and each student's browser draws them above the video.

The video takes the shape of the space the screen has on the stage. Alone it is
wide; beside a board it is tall, so the class sees a tall slice of the page.

Safety. The lecturer chooses the addresses, and later a model will, and a page can
ask for anything. A browser on our server must not be steered into our own network.

- Every request the browser makes (pages, pictures, scripts, frames) is held until
  its host has been checked, and refused unless the host is on the public internet.
- Downloads are refused.
- Chrome is driven over a private pipe, not a network port, so nothing else on the
  machine can drive it, and it exits when the lecturer does.

What this does not cover: WebSockets a page opens, a host that answers with a public
address when checked and a private one a moment later, the server's own public
address, the first requests of a cross-site frame or worker before its guard is in
place, frames inside such frames, and service workers. Run this somewhere with nothing
private to reach.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import logging
import os
import shutil
import socket
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import av
from livekit import rtc

logger = logging.getLogger("lecturer.screen")

FPS = 8
MAX_TABS = 12
MAX_ADDRESS, MAX_TITLE = 300, 120
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class ScreenError(RuntimeError):
    pass


def find_chrome() -> str:
    """CH_CHROME if set; else a Chrome or Chromium on the PATH; else where a Mac keeps it."""
    chosen = os.environ.get("CH_CHROME", "").strip()
    if chosen:
        if os.path.exists(chosen):
            return chosen
        raise ScreenError(f"CH_CHROME is {chosen!r}, and there is nothing there")
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if path := shutil.which(name):
            return path
    if os.path.exists(MAC_CHROME):
        return MAC_CHROME
    raise ScreenError("no Chrome found: set CH_CHROME to its path")


async def is_public_address(url: str) -> bool:
    """True if the address is http(s) and its host is on the public internet."""
    if "\\" in url or any(ord(c) < 33 for c in url):
        # Browsers read a backslash as a slash, and this parser does not: they could
        # disagree about which host is meant.
        return False
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        loop = asyncio.get_running_loop()
        found = await loop.getaddrinfo(parts.hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not found:
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in found)


def shape_for(columns: int, rows: int) -> tuple[int, int, int, int, float]:
    """The video size, page size and enlargement for a pane on a stage that is
    split into this many columns (in the screen's row) and rows.

    The video is the pane's shape. The page is laid out a little narrower than the
    video and enlarged to fill it, so its text is big enough to read in class.
    """
    width, height = 1920 / max(columns, 1), 1080 / max(rows, 1)
    fit = min(1.0, 1280 / width, 1080 / height)
    video_w, video_h = int(width * fit) // 2 * 2, int(height * fit) // 2 * 2
    page_w = round(min(video_w, max(video_w / 1.25, 600)))
    scale = video_w / page_w
    return video_w, video_h, page_w, round(video_h / scale), scale


@dataclass
class Tab:
    target: str
    session: str
    url: str = ""
    title: str = ""
    used: bool = False  # False for the empty tab the browser starts with


class ScreenShare:
    def __init__(self, room: rtc.Room) -> None:
        self._room = room
        self.on_change: Callable[[], None] | None = None  # tabs changed: tell the class
        self.tabs: list[Tab] = []
        self.active = 0  # index into tabs
        self.failed = False  # the browser could not be started, or it died
        self._shape = shape_for(1, 1)
        self._chrome: asyncio.subprocess.Process | None = None
        self._profile: str | None = None
        self._to_chrome: int | None = None  # the pipe we write commands to
        self._running = False
        self._next_id = 0
        self._waiting: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._source: rtc.VideoSource | None = None
        self._published = False
        self._jpeg: bytes | None = None  # the newest picture, not yet decoded
        self._frame: rtc.VideoFrame | None = None
        self._decoder = av.CodecContext.create("mjpeg", "r")
        self._start_lock = asyncio.Lock()
        self._hosts: dict[str, tuple[bool, float]] = {}  # host -> allowed, when checked
        self._change_due = False
        self._errors: list[str] = []  # the last few lines Chrome printed
        self._death: asyncio.Task[None] | None = None
        self.frames_sent = 0
        self._showing = False  # True once a tab's picture is being sent

    @property
    def started(self) -> bool:
        return self._running

    def state(self) -> dict[str, Any]:
        """What a browser needs to draw the tab strip and the address."""
        used = [t for t in self.tabs if t.used]
        current = self.tabs[self.active] if self.tabs else None
        return {
            "on": bool(used),
            "tabs": [{"title": t.title, "url": t.url} for t in used],
            "active": used.index(current) if current in used else 0,
            "failed": self.failed,
        }

    # -- what the lecturer can do ---------------------------------------------------

    async def open(self, url: str) -> None:
        """Open an address in a new tab and switch to it."""
        tab = await self._load(url)
        await self._activate(self.tabs.index(tab))
        self._changed()

    async def prepare(self, url: str) -> None:
        """Open an address in a new tab, ready for later, without switching to it."""
        await self._load(url)
        self._changed()

    async def switch(self, number: int) -> None:
        """Switch to a tab by its number, counting the tabs in use from 1."""
        used = [t for t in self.tabs if t.used]
        if not 1 <= number <= len(used):
            raise ScreenError(f"there is no tab {number}")
        await self._activate(self.tabs.index(used[number - 1]))
        self._changed()

    async def scroll(self, pixels: int) -> None:
        if not self.started or not self.tabs:
            return
        script = f"window.scrollBy({{top: {int(pixels)}, behavior: 'smooth'}})"
        await self._call("Runtime.evaluate", {"expression": script}, self.tabs[self.active])

    async def set_shape(self, columns: int, rows: int) -> None:
        """Tell the screen how the stage is split, so the video fits its pane."""
        shape = shape_for(columns, rows)
        changed, self._shape = shape != self._shape, shape
        # Also covers a tab that was prepared but has not been shown yet.
        if self.started and self.tabs and (changed or not self._showing):
            await self._show(self.tabs[self.active])

    async def hide(self) -> None:
        """The screen has left the stage: stop sending its picture."""
        if self._showing and self.started and self.tabs:
            self._showing = False
            self._jpeg = self._frame = None
            with contextlib.suppress(Exception):
                await self._call("Page.stopScreencast", tab=self.tabs[self.active])

    async def close(self) -> None:
        self._running = False
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current:
                task.cancel()
        self._tasks.clear()
        for waiting in self._waiting.values():
            if not waiting.done():
                waiting.set_exception(ScreenError("the shared screen has closed"))
        self._waiting.clear()
        if self._to_chrome is not None:
            with contextlib.suppress(OSError):
                os.close(self._to_chrome)  # Chrome exits when its command pipe closes
            self._to_chrome = None
        if self._chrome and self._chrome.returncode is None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._chrome.wait(), timeout=3)
            if self._chrome.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self._chrome.kill()
        self._chrome = None
        if self._profile:
            shutil.rmtree(self._profile, ignore_errors=True)
            self._profile = None
        self.tabs, self.active = [], 0
        self._showing = False
        self._jpeg = self._frame = None

    # -- tabs -------------------------------------------------------------------------

    async def _load(self, url: str) -> Tab:
        if not await is_public_address(url):
            raise ScreenError(f"refused: {url} is not a public web address")
        await self._start()
        tab = next((t for t in self.tabs if not t.used), None)
        if tab is None:
            if len(self.tabs) >= MAX_TABS:
                raise ScreenError(f"{MAX_TABS} tabs are open already")
            made = await self._call("Target.createTarget", {"url": "about:blank"})
            tab = await self._attach(made["targetId"])
        tab.used, tab.url = True, url[:MAX_ADDRESS]
        tab.title = urlsplit(url).hostname or url[:MAX_TITLE]
        result = await self._call("Page.navigate", {"url": url}, tab)
        if result.get("errorText"):
            tab.title = "This page did not load"
            raise ScreenError(f"{url} did not load: {result['errorText']}")
        return tab

    async def _attach(self, target: str) -> Tab:
        attached = await self._call("Target.attachToTarget", {"targetId": target, "flatten": True})
        tab = Tab(target, attached["sessionId"])
        self.tabs.append(tab)
        await self._call("Page.enable", tab=tab)
        await self._guard(tab.session)
        # Frames from other sites run as separate targets. Attach to them too, so
        # that what they load is checked like everything else.
        await self._call(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            tab,
        )
        return tab

    async def _guard(self, session: str) -> None:
        """Hold every request this target makes until we have checked where it goes."""
        await self._call("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session=session)

    async def _activate(self, index: int) -> None:
        old = self.tabs[self.active] if self.tabs else None
        new = self.tabs[index]
        if old is not None and old is not new:
            with contextlib.suppress(Exception):
                await self._call("Page.stopScreencast", tab=old)
        self.active = index
        await self._show(new)

    async def _show(self, tab: Tab) -> None:
        """Size the tab for its pane and send its picture."""
        video_w, video_h, page_w, page_h, scale = self._shape
        self._showing = True
        with contextlib.suppress(Exception):
            await self._call("Page.stopScreencast", tab=tab)
        await self._call("Page.bringToFront", tab=tab)
        await self._call(
            "Emulation.setDeviceMetricsOverride",
            {"width": page_w, "height": page_h, "deviceScaleFactor": scale, "mobile": False},
            tab,
        )
        await self._call(
            "Page.startScreencast",
            {"format": "jpeg", "quality": 80, "maxWidth": video_w, "maxHeight": video_h},
            tab,
        )

    async def _read_title(self, tab: Tab) -> None:
        """A tab's title, after it has loaded. A page chooses its own title, as in any
        browser, so it is kept short and treated as text."""
        with contextlib.suppress(Exception):
            got = await self._call("Runtime.evaluate", {"expression": "document.title"}, tab)
            title = str(got["result"].get("value") or "")[:MAX_TITLE]
            title = title or urlsplit(tab.url).hostname or tab.url
            if title != tab.title:
                tab.title = title
                self._changed()

    def _changed(self) -> None:
        """Tell the class the tabs changed, at most twice a second."""
        if self._change_due or not self.on_change:
            return
        self._change_due = True

        def send() -> None:
            self._change_due = False
            if self.on_change:
                self.on_change()

        asyncio.get_running_loop().call_later(0.5, send)

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- the browser ------------------------------------------------------------------

    async def _start(self) -> None:
        async with self._start_lock:
            if self.started:
                return
            try:
                await self._launch()
                self.failed = False
            except Exception as e:
                logger.error("the shared screen could not start: %s", e)
                await self.close()
                self.failed = True
                self._changed()
                raise

    async def _launch(self) -> None:
        video_w, video_h = self._shape[0], self._shape[1]
        self._profile = tempfile.mkdtemp(prefix="ch-screen-")
        # Chrome reads commands on its descriptor 3 and writes replies on 4. A small
        # shell line puts our two pipes there. With a pipe there is no network port
        # for anything else to connect to, and Chrome exits when the pipe closes,
        # which it does if this process dies for any reason.
        to_chrome_r, to_chrome_w = os.pipe()
        from_chrome_r, from_chrome_w = os.pipe()
        flags = [
            "--headless=new",
            "--remote-debugging-pipe",
            f"--user-data-dir={self._profile}",
            "--window-size=1280,1080",
            "--hide-scrollbars",
            "--mute-audio",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "about:blank",
        ]
        try:
            chrome = find_chrome()
        except ScreenError:
            for fd in (to_chrome_r, to_chrome_w, from_chrome_r, from_chrome_w):
                os.close(fd)
            raise
        try:
            self._chrome = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                f'exec "$0" "$@" 3<&{to_chrome_r} 4>&{from_chrome_w}',
                chrome,
                *flags,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(to_chrome_r, from_chrome_w),
            )
        finally:
            os.close(to_chrome_r)
            os.close(from_chrome_w)
        self._to_chrome = to_chrome_w
        self._errors = []
        assert self._chrome.stderr
        self._spawn(self._drain(self._chrome.stderr))

        loop = asyncio.get_running_loop()
        replies = asyncio.StreamReader(limit=64 * 1024 * 1024)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(replies), os.fdopen(from_chrome_r, "rb", 0)
        )
        self._running = True
        self._spawn(self._read(replies))

        try:
            targets = (await self._call("Target.getTargets", timeout=15))["targetInfos"]
        except Exception as e:
            reason = " ".join(self._errors[-3:]) or str(e)
            raise ScreenError(f"Chrome did not start: {reason}") from None
        with contextlib.suppress(ScreenError):
            await self._call("Browser.setDownloadBehavior", {"behavior": "deny"})
        page = next(t for t in targets if t["type"] == "page")
        await self._attach(page["targetId"])

        if self._source is None:
            self._source = rtc.VideoSource(video_w, video_h, is_screencast=True)
        if not self._published:
            track = rtc.LocalVideoTrack.create_video_track("lecturer-screen", self._source)
            options = rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_SCREENSHARE,
                simulcast=False,
                video_encoding=rtc.VideoEncoding(max_bitrate=1_800_000, max_framerate=FPS),
            )
            await self._room.local_participant.publish_track(track, options)
            self._published = True
        self._spawn(self._publish())

    async def _drain(self, stream: asyncio.StreamReader) -> None:
        """Keep the last few lines Chrome prints, in case it will not start."""
        async for raw in stream:
            self._errors = [*self._errors[-4:], raw.decode(errors="replace").strip()[:200]]

    async def _call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        tab: Tab | None = None,
        *,
        session: str | None = None,
        timeout: float = 20,
    ) -> dict[str, Any]:
        """Send one command to the browser, or to a tab of it, and wait for the reply."""
        if not self._running or self._to_chrome is None:
            raise ScreenError("the shared screen is not running")
        self._next_id += 1
        msg: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if tab is not None or session is not None:
            msg["sessionId"] = tab.session if tab is not None else session
        reply: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._waiting[msg["id"]] = reply
        try:
            os.write(self._to_chrome, json.dumps(msg).encode() + b"\0")
            return await asyncio.wait_for(reply, timeout=timeout)
        except OSError:
            raise ScreenError("the shared screen has stopped") from None
        finally:
            self._waiting.pop(msg["id"], None)

    async def _read(self, replies: asyncio.StreamReader) -> None:
        try:
            while True:
                raw = await replies.readuntil(b"\0")
                self._on_message(json.loads(raw[:-1]))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            pass
        except asyncio.CancelledError:
            raise
        # The pipe closed: Chrome has gone. Say so, and be ready to start again.
        if self._running:
            logger.error("the shared screen's browser stopped")
            self._death = asyncio.create_task(self._died())

    async def _died(self) -> None:
        await self.close()
        self.failed = True
        self._changed()

    def _on_message(self, msg: dict[str, Any]) -> None:
        if "id" in msg:
            reply = self._waiting.get(msg["id"])
            if reply and not reply.done():
                if "error" in msg:
                    reply.set_exception(ScreenError(msg["error"].get("message", "error")))
                else:
                    reply.set_result(msg.get("result", {}))
            return
        session = msg.get("sessionId", "")
        method, params = msg.get("method"), msg.get("params", {})
        if method == "Fetch.requestPaused":
            self._spawn(self._check_request(session, params))
            return
        if method == "Target.attachedToTarget":
            # A frame from another site. Guard it as well.
            child = params.get("sessionId")
            if child:
                self._spawn(self._guard_quietly(child))
            return
        tab = next((t for t in self.tabs if t.session == session), None)
        if tab is None:
            return
        if method == "Page.screencastFrame":
            self._on_frame(tab, params)
        elif method == "Page.frameNavigated" and not params.get("frame", {}).get("parentId"):
            # The address comes from the browser, not from the page's own script.
            url = str(params["frame"].get("url", ""))
            if url.startswith("http") and url[:MAX_ADDRESS] != tab.url:
                tab.url = url[:MAX_ADDRESS]
                self._changed()
        elif method == "Page.loadEventFired":
            self._spawn(self._read_title(tab))

    async def _guard_quietly(self, session: str) -> None:
        with contextlib.suppress(Exception):
            await self._guard(session)

    async def _allowed(self, url: str) -> bool:
        if url.startswith(("about:", "data:", "blob:")):
            return True
        host = urlsplit(url).hostname or ""
        known = self._hosts.get(host)
        if known and time.monotonic() - known[1] < 60:
            return known[0]
        allowed = await is_public_address(url)
        if len(self._hosts) > 2000:
            self._hosts.clear()
        self._hosts[host] = (allowed, time.monotonic())
        return allowed

    async def _check_request(self, session: str, params: dict[str, Any]) -> None:
        url = params["request"]["url"]
        allowed = await self._allowed(url)
        with contextlib.suppress(Exception):
            if allowed:
                await self._call(
                    "Fetch.continueRequest", {"requestId": params["requestId"]}, session=session
                )
            else:
                logger.warning("the shared screen refused to load %s", url[:200])
                await self._call(
                    "Fetch.failRequest",
                    {"requestId": params["requestId"], "errorReason": "AccessDenied"},
                    session=session,
                )

    # -- the picture ------------------------------------------------------------------

    def _on_frame(self, tab: Tab, params: dict[str, Any]) -> None:
        self._spawn(self._ack(tab, params["sessionId"]))
        if self._showing and self.tabs and tab is self.tabs[self.active]:
            # Only the newest picture is kept. It is decoded when it is sent, so a
            # page that changes fast does not cost a decode for every frame.
            self._jpeg = base64.b64decode(params["data"])

    async def _ack(self, tab: Tab, frame: int) -> None:
        with contextlib.suppress(Exception):
            await self._call("Page.screencastFrameAck", {"sessionId": frame}, tab)

    def _decode(self, jpeg: bytes) -> rtc.VideoFrame | None:
        try:
            decoded = self._decoder.decode(av.Packet(jpeg))
            if not decoded:
                return None
            picture = decoded[0]
            # Video sizes must be even.
            width, height = picture.width // 2 * 2, picture.height // 2 * 2
            if (width, height) != (picture.width, picture.height):
                picture = picture.reformat(width=width, height=height)
            rgba = picture.to_ndarray(format="rgba")
            return rtc.VideoFrame(width, height, rtc.VideoBufferType.RGBA, rgba.tobytes())
        except Exception:
            logger.exception("could not decode a frame of the shared screen")
            return None

    async def _publish(self) -> None:
        """Send the latest picture at a steady rate, so a still page stays on screen."""
        assert self._source
        while True:
            if self._jpeg is not None:
                jpeg, self._jpeg = self._jpeg, None
                self._frame = self._decode(jpeg) or self._frame
            if self._frame is not None and self._showing:
                self._source.capture_frame(self._frame)
                self.frames_sent += 1
            await asyncio.sleep(1 / FPS)
