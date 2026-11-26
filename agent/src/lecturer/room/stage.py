"""The stage: what students are looking at.

The lecturer chooses what is on it: the board, slides, a shared screen, or two of
them side by side. Like the board, the stage is changed only by events, so a
browser can rebuild it from a snapshot. The browser has the same rules in
`web/lib/classroom/store.ts`; keep the two in step.

The shared screen's picture is a video track. Only whether it is on, and the
address it shows, are kept here.

A video (a file from lesson prep, or one on YouTube or Twitch) is not sent by the
lecturer at all. Each browser plays it for itself, and the lecturer only says which
video, whether it is playing, and from what moment. So it plays smoothly and with its
sound, and someone who joins late is put at the right moment. There is one video at a
time. It is seen in a `video` pane of the stage, or in a slide that holds it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..script.deck import load_deck
from ..script.media import video_ref
from ..script.steps import Step
from .board import picture_event, reveal_ms


@dataclass
class Stage:
    # Rows of panes, left to right and top to bottom. A pane is a source, and may
    # be fixed to one slide or one board page.
    rows: list[list[dict[str, Any]]] = field(default_factory=lambda: [[{"source": "board"}]])
    deck: dict[str, Any] | None = None
    slide: int = 1  # the number of the slide showing, from 1
    # Per slide: what the lecturer has written on it, and marks on its named parts.
    ink: dict[int, list[dict[str, Any]]] = field(default_factory=dict)
    marks: dict[int, dict[str, str]] = field(default_factory=dict)
    # The shared screen's tabs, as a browser's tab strip would show them.
    screen: dict[str, Any] = field(
        default_factory=lambda: {"on": False, "tabs": [], "active": 0, "failed": False}
    )
    # The video: which one (None if none is loaded), whether it is playing, and the
    # moment it was at when it last started or stopped. `quiet`: the lecturer is
    # talking over it, so browsers turn it down.
    video: dict[str, Any] = field(
        default_factory=lambda: {"src": None, "playing": False, "position": 0.0, "quiet": False}
    )
    _since: float = 0.0  # when it last started playing, by this machine's clock

    def video_position(self) -> float:
        """The moment the video is at now, in seconds."""
        position = float(self.video["position"])
        if self.video["playing"]:
            position += max(0.0, time.time() - self._since)
        return round(position, 1)

    def video_in_view(self) -> dict[str, Any] | None:
        """The video the class can see: the one in the video pane, or the one on a
        slide that is showing. `play:` means this one."""
        panes = [pane for row in self.rows for pane in row]
        if not any(pane["source"] == "video" for pane in panes) and self.deck:
            for pane in panes:
                if pane["source"] != "slides":
                    continue
                n = pane.get("slide", self.slide)
                if 1 <= n <= self.slide_count():
                    for item in self.deck["slides"][n - 1]["items"]:
                        if item["kind"] == "video":
                            return dict(item["video"])
        return self.video["src"]

    def snapshot(self) -> dict[str, Any]:
        return {
            "video": {**self.video, "position": self.video_position()},
            "rows": [[dict(pane) for pane in row] for row in self.rows],
            "deck": self.deck,
            "slide": self.slide,
            "ink": {str(n): lines for n, lines in self.ink.items() if lines},
            "marks": {str(n): marks for n, marks in self.marks.items() if marks},
            "screen": dict(self.screen),
        }

    @classmethod
    def from_snapshot(cls, snap: dict[str, Any]) -> Stage:
        return cls(
            rows=[[dict(pane) for pane in row] for row in snap["rows"]],
            deck=snap["deck"],
            slide=snap["slide"],
            ink={int(n): lines for n, lines in snap["ink"].items()},
            marks={int(n): marks for n, marks in snap["marks"].items()},
            screen=dict(snap["screen"]),
            video=dict(snap.get("video") or cls().video),
            _since=time.time(),
        )

    def slide_count(self) -> int:
        return len(self.deck["slides"]) if self.deck else 0

    def apply(self, ev: dict[str, Any]) -> None:
        t = ev["type"]
        if t == "stage.show":
            self.rows = [[dict(pane) for pane in row] for row in ev["rows"]]
        elif t == "slides.load":
            self.deck, self.slide, self.ink, self.marks = ev["deck"], 1, {}, {}
        elif t == "slides.go":
            if 1 <= ev["n"] <= self.slide_count():
                self.slide = ev["n"]
        elif t == "slides.write":
            line = {k: ev[k] for k in ("id", "kind", "content", "area")}
            if ev["kind"] == "image":
                line.update(alt=ev.get("alt", ""), pending=ev.get("pending", False), failed=False)
            self.ink.setdefault(self.slide, []).append({**line, "marks": {}})
        elif t == "slides.fill":
            # The picture may be ready after the lecturer has moved to another slide.
            for lines in self.ink.values():
                for line in lines:
                    if line["id"] == ev["id"] and line.get("pending"):
                        line.update(
                            content=ev.get("content", ""),
                            pending=False,
                            failed=bool(ev.get("failed")),
                        )
        elif t == "video.load":
            self.video = {
                "src": ev["src"],
                "playing": False,
                "position": float(ev.get("position", 0)),
                "quiet": False,
            }
        elif t == "video.play":
            self.video = {
                **self.video,
                "playing": True,
                "position": float(ev["position"]),
                "quiet": bool(ev.get("quiet")),
            }
            self._since = float(ev.get("t") or time.time())
        elif t == "video.stop":
            self.video = {
                **self.video,
                "playing": False,
                "position": float(ev["position"]),
                "quiet": False,
            }
        elif t == "slides.mark":
            self._mark(ev)
        elif t == "screen.state":
            self.screen = {
                "on": ev["on"],
                "tabs": ev.get("tabs", []),
                "active": ev.get("active", 0),
                "failed": ev.get("failed", False),
            }

    def _mark(self, ev: dict[str, Any]) -> None:
        style, part = ev["style"], ev.get("part", "")
        if ev.get("id"):  # a line the lecturer wrote on the slide
            for line in self.ink.get(self.slide, []):
                if line["id"] == ev["id"]:
                    if style == "clear":
                        line["marks"].pop(part, None)
                    else:
                        line["marks"][part] = style
        elif ev.get("name"):  # a named item or area of the slide itself
            marks = self.marks.setdefault(self.slide, {})
            if style == "clear":
                marks.pop(ev["name"], None)
            else:
                marks[ev["name"]] = style
        elif style == "clear":
            self.marks.pop(self.slide, None)
            for line in self.ink.get(self.slide, []):
                line["marks"].clear()


def stage_events(
    step: Step, stage: Stage, sentence_ms: float | None = None, folder: str = ""
) -> list[dict[str, Any]]:
    """Turn one stage step into the events sent to browsers. `folder` is the lesson's
    own folder, where a file named without one is looked for."""
    if step.verb == "video":
        ref = video_ref(step.text, folder)
        if ref is None:
            raise ValueError(f"not a video that can be shown: {step.text!r}")
        return [{"type": "video.load", "src": ref.to_json(), "position": ref.start}]
    if step.verb == "play":
        out: list[dict[str, Any]] = []
        src = stage.video_in_view()
        if src is None:
            raise ValueError("there is no video to play")
        position = stage.video_position()
        if src != stage.video["src"]:
            # The video on the slide showing: it becomes the one video.
            out.append({"type": "video.load", "src": src, "position": 0})
            position = 0.0
        if step.start >= 0:
            position = step.start
        # With no time to watch it, the lecturer goes on talking, so it is turned down.
        out.append({"type": "video.play", "position": position, "quiet": step.seconds <= 0})
        return out
    if step.verb == "stop":
        if stage.video["src"] is None or not stage.video["playing"]:
            return []
        return [{"type": "video.stop", "position": stage.video_position()}]
    if step.verb in ("image", "generate"):
        return [
            {
                "type": "slides.write",
                "id": step.id,
                "area": step.area,
                **picture_event(step, folder),
                "reveal_ms": 400,
            }
        ]
    if step.verb == "show":
        return [{"type": "stage.show", "rows": [[dict(pane) for pane in row] for row in step.rows]}]
    if step.verb == "slides":
        return [{"type": "slides.load", "deck": load_deck(step.text)}]
    if step.verb == "slide":
        if step.text == "next":
            n = stage.slide + 1
        elif step.text == "back":
            n = stage.slide - 1
        else:
            n = int(step.text)
        n = min(max(n, 1), max(stage.slide_count(), 1))
        return [{"type": "slides.go", "n": n}]
    if step.verb in ("write", "note"):
        return [
            {
                "type": "slides.write",
                "id": step.id,
                "kind": "math" if step.verb == "write" else "text",
                "content": step.text,
                "area": step.area,
                "reveal_ms": reveal_ms(step.text, sentence_ms),
            }
        ]
    if step.verb == "mark":
        ev: dict[str, Any] = {"type": "slides.mark", "style": step.style}
        if step.id == "slide":
            if step.part:
                ev["name"] = step.part
        else:
            ev["id"] = step.id
            if step.part:
                ev["part"] = step.part
        return [ev]
    raise ValueError(f"not a stage step: {step.verb}")
