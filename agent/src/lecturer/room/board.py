"""Board state, and the events that change it.

The board is never sent as a picture. Each browser draws it from these events, in
order, and can ask for a snapshot when it joins or loses its place. The browser has
the same rules in `web/lib/classroom/store.ts`; keep the two in step.

Auto layout: lines stack top to bottom in the order they are written. The lecturer
can also leave a gap before a line, and start a second column level with a line.
Pages are kept when the board is wiped, and an earlier page can be shown again.

A line can be a picture: a file from lesson prep, or one made during the class by the
image model. A made picture takes a moment, so its line is written at once, empty
(`pending`), and filled in by a later event when the picture is ready.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..script.media import picture_file
from ..script.steps import Step


@dataclass
class Line:
    id: str
    kind: str  # "math" | "text" | "title" | "image"
    content: str  # image: where browsers fetch it, such as lesson-files/<folder>/<file>
    marks: dict[str, str] = field(default_factory=dict)  # part ("" = whole line) -> style
    col: int = 0  # 0 is the first column, 1 the second
    beside: str = ""  # the line this one starts level with (second column)
    space: bool = False  # a gap above, to set a new problem apart
    alt: str = ""  # image: what it is a picture of, in words
    pending: bool = False  # image: it is being made, and is not there yet
    failed: bool = False  # image: it could not be made

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "content": self.content,
            "marks": self.marks,
            "col": self.col,
            "beside": self.beside,
            "space": self.space,
        }
        if self.kind == "image":
            out.update(alt=self.alt, pending=self.pending, failed=self.failed)
        return out

    def describe(self) -> str:
        """The line in words, for a model that is shown the board."""
        if self.kind != "image":
            return self.content
        state = " (still being made)" if self.pending else ""
        state = " (could not be made: nothing is shown)" if self.failed else state
        return f"a picture: {self.alt}{state}"


@dataclass
class Board:
    """All the pages written so far, and which one is showing.

    A wipe starts a new page and keeps the old one, so the lecturer can bring it
    back instead of writing it again.
    """

    page: int = 0  # the page showing now
    top: int = 0  # the newest page's number
    pages: dict[int, list[Line]] = field(default_factory=lambda: {0: []})

    @property
    def lines(self) -> list[Line]:
        return self.pages[self.page]

    def snapshot(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "top": self.top,
            "lines": [ln.to_json() for ln in self.lines],
            # the other pages that have anything on them
            "saved": {
                str(n): [ln.to_json() for ln in lines]
                for n, lines in self.pages.items()
                if n != self.page and lines
            },
        }

    @classmethod
    def from_snapshot(cls, snap: dict[str, Any]) -> Board:
        def lines(raw: list[dict[str, Any]]) -> list[Line]:
            return [
                Line(
                    ln["id"],
                    ln["kind"],
                    ln["content"],
                    dict(ln["marks"]),
                    ln.get("col", 0),
                    ln.get("beside", ""),
                    ln.get("space", False),
                    ln.get("alt", ""),
                    ln.get("pending", False),
                    ln.get("failed", False),
                )
                for ln in raw
            ]

        pages = {int(n): lines(raw) for n, raw in snap.get("saved", {}).items()}
        pages[snap["page"]] = lines(snap["lines"])
        return cls(page=snap["page"], top=snap.get("top", snap["page"]), pages=pages)

    def _find(self, line_id: str) -> Line | None:
        return next((ln for ln in self.lines if ln.id == line_id), None)

    def apply(self, ev: dict[str, Any]) -> None:
        """Apply one board event. Unknown targets are ignored, as in the browser."""
        t = ev["type"]
        if t == "board.clear":
            self.top += 1
            self.page = self.top
            self.pages[self.page] = []
        elif t == "board.show":
            if ev["page"] in self.pages:
                self.page = ev["page"]
        elif t == "board.write":
            self.lines.append(
                Line(
                    ev["id"],
                    ev["kind"],
                    ev["content"],
                    col=ev.get("col", 0),
                    beside=ev.get("beside", ""),
                    space=ev.get("space", False),
                    alt=ev.get("alt", ""),
                    pending=ev.get("pending", False),
                )
            )
        elif t == "board.fill":
            # The picture may be ready after its page was wiped: look on every page.
            for lines in self.pages.values():
                for ln in lines:
                    if ln.id == ev["id"] and ln.pending:
                        ln.content, ln.pending = ev.get("content", ""), False
                        ln.failed = bool(ev.get("failed"))
        elif t == "board.erase":
            self.pages[self.page] = [ln for ln in self.lines if ln.id != ev["id"]]
        elif t == "board.mark":
            if ev["style"] == "clear" and not ev.get("id"):
                for ln in self.lines:
                    ln.marks.clear()
            elif line := self._find(ev.get("id", "")):
                part = ev.get("part", "")
                if ev["style"] == "clear":
                    line.marks.pop(part, None)
                else:
                    line.marks[part] = ev["style"]


def reveal_ms(content: str, sentence_ms: float | None) -> int:
    """How long a line takes to appear, left to right.

    With a sentence, it is written across most of that sentence. Alone, by length.
    """
    by_length = 350 + 45 * len(content)
    if sentence_ms:
        return int(min(max(sentence_ms * 0.85, 500), 5000))
    return int(min(max(by_length, 500), 3000))


def picture_event(step: Step, folder: str) -> dict[str, Any]:
    """What a picture step writes, on the board or on a slide: the picture's line."""
    if step.verb == "generate":
        return {"kind": "image", "content": "", "alt": step.text, "pending": True}
    file = picture_file(step.text, folder)
    if file is None:
        raise ValueError(f"no folder is known for the picture {step.text!r}")
    return {"kind": "image", "content": f"lesson-files/{file}", "alt": step.text}


def board_events(
    step: Step, sentence_ms: float | None = None, folder: str = ""
) -> list[dict[str, Any]]:
    """Turn one board step into the events sent to browsers. `folder` is the lesson's
    own folder, where a picture named without one is looked for."""
    if step.verb in ("image", "generate"):
        return [
            {
                "type": "board.write",
                "id": step.id,
                **picture_event(step, folder),
                "reveal_ms": 400,
                "col": step.col,
                "beside": step.beside,
                "space": step.space,
            }
        ]
    if step.verb == "write":
        return [
            {
                "type": "board.write",
                "id": step.id,
                "kind": "math",
                "content": step.text,
                "reveal_ms": reveal_ms(step.text, sentence_ms),
                "col": step.col,
                "beside": step.beside,
                "space": step.space,
            }
        ]
    if step.verb == "note":
        return [
            {
                "type": "board.write",
                "id": step.id,
                "kind": "text",
                "content": step.text,
                "reveal_ms": reveal_ms(step.text, sentence_ms),
                "col": step.col,
                "beside": step.beside,
                "space": step.space,
            }
        ]
    if step.verb == "title":
        return [
            {"type": "board.clear"},
            {
                "type": "board.write",
                "id": "_title",
                "kind": "title",
                "content": step.text,
                "reveal_ms": 400,
            },
        ]
    if step.verb == "clear":
        return [{"type": "board.clear"}]
    if step.verb == "page":
        return [{"type": "board.show", "page": step.page}]
    if step.verb == "erase":
        return [{"type": "board.erase", "id": step.id}]
    if step.verb == "mark":
        ev: dict[str, Any] = {"type": "board.mark", "style": step.style}
        if step.id:
            ev["id"] = step.id
        if step.part:
            ev["part"] = step.part
        return [ev]
    raise ValueError(f"not a board step: {step.verb}")
