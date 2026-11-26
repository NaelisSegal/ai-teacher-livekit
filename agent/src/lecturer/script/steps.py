"""The say / write / mark stream.

A lesson script and (later) the live writer both produce the same thing: lines of
steps, speaking and board actions mixed together.

    say: Let's call this function g.
    write g: g(x) = (\\part{inner}{3x + 1})^5
    say: The outside is "something to the fifth", and the inside is 3x plus 1.
    mark: highlight g.inner
    pause: 20 Try it yourself.

Verbs:
    say: <one or more sentences>
    write [id]: <LaTeX>            an equation line
    note [id]: <plain text>        a line of ordinary text
    title: <plain text>            wipes the board and starts a new page
    page: <an earlier title>       brings that page back (wiped pages are kept)
    mark: <style> [id[.part]]      styles: highlight, underline, box, dim, clear
    erase: <id>
    clear:
    gap:                           leaves a space before the next line, to set a new
                                   problem apart from the one above it
    column: right [beside <id>]    the next lines go in a second column, starting level
                                   with that line (or at the top) and going down from there
    column: left                   back to the first column
    pause: <seconds> [what the class should do]

The stage (what students look at) has more than the board:
    show: board                    what is on the stage: board, slides or screen,
    show: slides | board           or several side by side, left to right as written,
    show: slides | board / screen  with / starting a new row underneath
    show: slides 1 | slides 2      a pane can be fixed to one slide,
    show: board Rates multiply | board   or to one board page, by its title
    slides: <deck>                 loads a deck from lesson prep, as <course>/<deck>
    slide: <number | next | back>
    write [id] @<area>: <LaTeX>    writes in a named area of the slide showing
    note [id] @<area>: <text>
    mark: <style> slide.<name>     marks a named item or area of the slide
    open: <https address>          opens the address in a new tab of the shared screen
    prepare: <https address>       opens it in a new tab without switching to it
    tab: <number>                  switches to that tab
    scroll: <pixels>               the shared screen scrolls down (or up, if negative)

Pictures and videos (`media.py` says how files and addresses are written):
    image [id]: <file>             a picture from lesson prep, as a line on the board
    image [id] @<area>: <file>     or in a named area of the slide showing
    generate [id]: <description>   a picture made now, by the image model. Its place is
    generate [id] @<area>: ...     kept at once and the picture appears when it is ready
    video: <file | address>        loads a video: a file from lesson prep, or a YouTube
                                   or Twitch address
    show: video | board            the video is a source like the others; alone, it
                                   fills the stage. A slide can hold one too (`deck.py`)
    play:                          plays the video in view, while the lecturer carries on
    play: for <seconds>            plays it and waits that long while the class watches,
                                   then stops it
    play: at <time> [for <seconds>]    from that moment: 1:20, or a number of seconds
    stop:                          stops the video where it is

Groups (`room/groups.py`): the class splits, and each group has its own lecturer.
    split: <how> [<minutes>] | <title> = <what> | <title> = <what> ...
        how:   choose    each student picks a group
               random    students are put in groups by chance
               helper    a human helper puts each student in a group
        what:  a prepared lesson, as <course>/<lesson>, or an instruction for a
               lecturer who teaches the group live from this lesson's prep
    split: one <student> [<minutes>] | <title> = <instruction>
        takes one student aside while the class goes on. For the live teacher only,
        and a helper must agree to it first.
    regroup: [<title>]             ends the groups, or that one, and brings them back
A split needs a human helper for each group. Without them it is not done.

Who may speak:
    mic: hands                     students raise a hand and are given the floor in turn
    mic: open [what was asked]     any student may unmute, to answer or discuss
    mic: closed                    only staff can speak

Polls, shown in the chat:
    poll: <question> | <option> | <option> ...    two to six options
    poll: close

A thinking pause ends early when every student has pressed done (a reaction).

Board and stage actions belong to the `say` just before them and play when that
sentence starts. Actions before any `say` play on their own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .media import is_picture_file, is_video
from .media import seconds as read_time

MARK_STYLES = ("highlight", "underline", "box", "dim", "clear")
STAGE_VERBS = ("show", "slides", "slide", "video", "play", "stop")
PICTURE_VERBS = ("image", "generate")
SPLITS = ("choose", "random", "helper", "one")
MIC_MODES = ("closed", "hands", "open")
SCREEN_VERBS = ("open", "prepare", "tab", "scroll")
SOURCES = ("board", "slides", "screen", "video")

_LINE = re.compile(
    r"^(?P<verb>[a-z]+)(?:[ \t]+(?P<id>[A-Za-z][\w-]*))?(?:[ \t]+@(?P<at>[A-Za-z][\w-]*))?"
    r"[ \t]*:[ \t]?(?P<rest>.*)$"
)
_DECK = re.compile(r"^[a-z0-9-]+/[a-z0-9-]+$")
_TARGET = re.compile(r"^(?P<id>[A-Za-z][\w-]*)(?:\.(?P<part>[A-Za-z][\w-]*))?$")


class StepError(ValueError):
    pass


def _number(text: str) -> bool:
    """Plain digits. (`isdigit` alone is true for some characters `int` cannot read.)"""
    return text.isascii() and text.isdigit()


@dataclass
class Step:
    verb: str
    text: str = ""  # say: sentence(s). write: LaTeX. note/title: plain text. pause: reason
    id: str = ""  # write/note: the line's id. mark/erase: the target line
    part: str = ""  # mark: a named part inside the line ("" means the whole line)
    style: str = ""  # mark
    seconds: float = 0.0  # pause
    page: int = 0  # page: the number of the page to show
    on: str = "board"  # write/note/mark: "board", or "slide" for the slide showing
    area: str = ""  # write/note on a slide: the named area to write in
    rows: list[list[dict[str, object]]] = field(default_factory=list)  # show: rows of panes
    options: list[str] = field(default_factory=list)  # poll: the choices
    amount: int = 0  # scroll: pixels. tab: the tab's number
    col: int = 0  # write/note: 0 is the first column, 1 the second
    beside: str = ""  # write/note: the line this one starts level with
    space: bool = False  # write/note: leave a gap above this line
    start: float = -1.0  # play: the moment to play from, in seconds (-1: where it is)
    groups: list[tuple[str, str]] = field(default_factory=list)  # split: title, what
    line_no: int = 0


@dataclass
class StepParser:
    """Parses step lines one at a time, so it works on a live stream too."""

    strict: bool = True  # scripts: raise on a bad line. live writer: skip it.
    _auto: int = 0
    _seen: set[str] = field(default_factory=set)  # line ids on the page showing
    _line_no: int = 0
    _top: int = 0  # the newest page's number
    _titled: dict[str, tuple[int, set[str]]] = field(default_factory=dict)  # title -> page
    _inked: set[str] = field(default_factory=set)  # line ids written on slides
    _col: int = 0  # the column being written in
    _beside: str = ""  # the line the next right-column line starts level with
    _space: bool = False  # a gap is owed before the next line

    def _new_page(self, title: str = "") -> None:
        self._top += 1
        self._seen = set()
        self._col, self._beside, self._space = 0, "", False
        if title:
            self._titled[title.casefold()] = (self._top, self._seen)

    def _stage_rows(self, text: str) -> list[list[dict[str, object]]]:
        """Read `slides 2 | board / screen` into rows of panes."""
        rows: list[list[dict[str, object]]] = []
        for row_text in text.split("/"):
            row: list[dict[str, object]] = []
            for pane_text in row_text.split("|"):
                words = pane_text.split(maxsplit=1)
                if not words or words[0] not in SOURCES:
                    raise self._fail(f"show: sources are {', '.join(SOURCES)}")
                pane: dict[str, object] = {"source": words[0]}
                if len(words) == 2:
                    which = words[1].strip()
                    if words[0] == "slides" and _number(which) and int(which) >= 1:
                        pane["slide"] = int(which)  # this slide, whichever is current
                    elif words[0] == "board" and which.casefold() in self._titled:
                        pane["page"] = self._titled[which.casefold()][0]  # that page
                    else:
                        raise self._fail(f"show: cannot show {pane_text.strip()!r}")
                row.append(pane)
            rows.append(row)
        panes = [pane for row in rows for pane in row]
        if len(panes) > 9:
            raise self._fail("show: nine things at most")
        if sum(1 for pane in panes if pane["source"] == "screen") > 1:
            raise self._fail("show: the shared screen can be shown once")
        if sum(1 for pane in panes if pane["source"] == "video") > 1:
            raise self._fail("show: the video can be shown once")
        return rows

    def feed(self, line: str) -> Step | None:
        self._line_no += 1
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        try:
            return self._parse(line)
        except StepError:
            if self.strict:
                raise
            return None
        except ValueError as e:
            # Something no check foresaw. A script is told; a live writer's line is skipped.
            if self.strict:
                raise self._fail(f"could not be read: {e}") from e
            return None

    def _fail(self, msg: str) -> StepError:
        return StepError(f"line {self._line_no}: {msg}")

    def _parse(self, line: str) -> Step | None:
        m = _LINE.match(line)
        if not m:
            raise self._fail(f"not a step: {line!r}")
        verb, ident, rest = m["verb"], m["id"] or "", m["rest"].strip()
        area = m["at"] or ""
        n = self._line_no
        if area and verb not in ("write", "note", *PICTURE_VERBS):
            raise self._fail("only write, note, image and generate take an @area")

        if verb == "say":
            if ident or not rest:
                raise self._fail("say takes text and no id")
            return Step("say", text=rest, line_no=n)

        if verb in ("write", "note", *PICTURE_VERBS):
            if not rest:
                raise self._fail(f"{verb} needs content")
            if verb == "image" and not is_picture_file(rest):
                raise self._fail("image takes a picture file from lesson prep, such as cell.jpg")
            if verb == "generate" and len(rest) > 600:
                raise self._fail("generate takes a description of 600 characters at most")
            # A part's name becomes an attribute in the page, so it must be a plain word.
            for name in re.findall(r"\\part\{([^}]*)\}", rest) if verb == "write" else []:
                if not re.fullmatch(r"[A-Za-z][\w-]*", name):
                    raise self._fail(f"a part name must be one plain word, not {name!r}")
            seen = self._inked if area else self._seen
            if not ident:
                self._auto += 1
                ident = f"_{self._auto}"
            elif ident in self._seen or ident in self._inked or ident == "slide":
                raise self._fail(f"id {ident!r} is already in use")
            seen.add(ident)
            if area:
                return Step(verb, text=rest, id=ident, on="slide", area=area, line_no=n)
            step = Step(verb, text=rest, id=ident, line_no=n)
            step.col, step.beside, step.space = self._col, self._beside, self._space
            self._beside, self._space = "", False
            return step

        if verb == "gap":
            if ident or rest:
                raise self._fail("gap takes nothing after it")
            self._space = True
            return None

        if verb == "column":
            words = rest.split()
            if ident or not words or words[0] not in ("left", "right"):
                raise self._fail("column takes left or right")
            if words[0] == "left":
                if len(words) != 1:
                    raise self._fail("column: left takes nothing after it")
                self._col, self._beside = 0, ""
                return None
            if len(words) == 1:
                self._col, self._beside = 1, ""
            elif len(words) == 3 and words[1] == "beside" and words[2] in self._seen:
                self._col, self._beside = 1, words[2]
            else:
                raise self._fail("column: right, or column: right beside <a line on the board>")
            return None

        if verb == "title":
            if ident or not rest:
                raise self._fail("title takes text and no id")
            self._new_page(rest)
            return Step("title", text=rest, line_no=n)

        if verb == "clear":
            self._new_page()
            return Step("clear", line_no=n)

        if verb == "page":
            found = self._titled.get(rest.casefold()) if not ident else None
            if not found:
                raise self._fail(f"page: no earlier page is titled {rest!r}")
            number, self._seen = found
            self._col, self._beside, self._space = 0, "", False
            return Step("page", text=rest, page=number, line_no=n)

        if verb == "erase":
            target = ident or rest
            if target not in self._seen:
                raise self._fail(f"erase: nothing called {target!r} on the board")
            self._seen.discard(target)
            return Step("erase", id=target, line_no=n)

        if verb == "mark":
            if ident:
                raise self._fail("mark is written as `mark: <style> <target>`")
            words = rest.split()
            if not words or words[0] not in MARK_STYLES:
                raise self._fail(f"mark style must be one of {', '.join(MARK_STYLES)}")
            style = words[0]
            if len(words) == 1:
                if style != "clear":
                    raise self._fail("mark needs a target")
                return Step("mark", style="clear", line_no=n)
            t = _TARGET.match(words[1]) if len(words) == 2 else None
            if not t:
                raise self._fail(f"bad mark target: {' '.join(words[1:])!r}")
            part = t["part"] or ""
            if t["id"] == "slide":
                # A named item or area of the slide showing. The deck is not known
                # here, so a wrong name is simply not drawn.
                return Step("mark", style=style, id="slide", part=part, on="slide", line_no=n)
            if t["id"] in self._inked:
                return Step("mark", style=style, id=t["id"], part=part, on="slide", line_no=n)
            if t["id"] not in self._seen:
                raise self._fail(f"mark: nothing called {t['id']!r} on the board")
            return Step("mark", style=style, id=t["id"], part=part, line_no=n)

        if verb == "show":
            rows = [] if ident else self._stage_rows(rest)
            if not rows:
                raise self._fail("show takes sources with | between them, and / between rows")
            return Step("show", rows=rows, line_no=n)

        if verb == "slides":
            if ident or not _DECK.match(rest):
                raise self._fail("slides takes a deck name, as <course>/<deck>")
            self._inked = set()
            return Step("slides", text=rest, line_no=n)

        if verb == "slide":
            if ident or not (rest in ("next", "back") or (_number(rest) and int(rest) >= 1)):
                raise self._fail("slide takes a number, next or back")
            return Step("slide", text=rest, line_no=n)

        if verb == "video":
            if ident or not is_video(rest):
                raise self._fail(
                    "video takes a video file from lesson prep, or a YouTube or Twitch address"
                )
            return Step("video", text=rest, line_no=n)

        if verb == "play":
            # play: [at <time>] [for <seconds>]
            words = rest.split()
            start, watch = -1.0, 0.0
            try:
                if ident or len(words) not in (0, 2, 4):
                    raise ValueError
                for name, value in zip(words[::2], words[1::2], strict=True):
                    amount = read_time(value)
                    if amount is None:
                        raise ValueError
                    if name == "at":
                        start = amount
                    elif name == "for" and 0 < amount <= 1800:
                        watch = amount
                    else:
                        raise ValueError
            except ValueError:
                raise self._fail("play is written as `play: [at <time>] [for <seconds>]`") from None
            return Step("play", start=start, seconds=watch, line_no=n)

        if verb == "stop":
            if ident or rest:
                raise self._fail("stop takes nothing after it")
            return Step("stop", line_no=n)

        if verb == "split":
            # split: <how> [<student>] [<minutes>] | <title> = <what> | ...
            pieces = [piece.strip() for piece in rest.split("|")]
            words = pieces[0].split()
            how = words[0] if words else ""
            who = words.pop(1) if how == "one" and len(words) > 1 else ""
            groups = [tuple(x.strip() for x in g.split("=", 1)) for g in pieces[1:]]
            try:
                if ident or how not in SPLITS or len(words) > 2 or (how == "one") != bool(who):
                    raise ValueError
                minutes = float(words[1]) if len(words) == 2 else (5 if how == "one" else 10)
                wanted = (1, 1) if how == "one" else (2, 6)
                if not 0 < minutes <= 120 or not wanted[0] <= len(groups) <= wanted[1]:
                    raise ValueError
                if any(
                    len(g) != 2 or not all(g) or len(g[0]) > 60 or len(g[1]) > 600 for g in groups
                ):
                    raise ValueError
                if len({g[0].casefold() for g in groups}) != len(groups):
                    raise ValueError
            except ValueError:
                raise self._fail(
                    "split is written as `split: choose|random|helper [minutes] | title = what "
                    "| title = what`, or `split: one <student> [minutes] | title = what`"
                ) from None
            return Step(
                "split",
                style=how,
                text=who,
                seconds=minutes * 60,
                groups=groups,
                line_no=n,  # type: ignore[arg-type]
            )

        if verb == "regroup":
            if ident:
                raise self._fail("regroup takes a group's title, or nothing")
            return Step("regroup", text=rest, line_no=n)

        if verb in ("open", "prepare"):
            if ident or not re.match(r"^https?://[^\s]+$", rest):
                raise self._fail(f"{verb} takes an http or https address")
            return Step(verb, text=rest, line_no=n)

        if verb == "tab":
            if ident or not (_number(rest) and 1 <= int(rest) <= 20):
                raise self._fail("tab takes the number of a tab, counting from 1")
            return Step("tab", amount=int(rest), line_no=n)

        if verb == "poll":
            if ident:
                raise self._fail("poll takes a question and options, with | between them")
            if rest == "close":
                return Step("poll", style="close", line_no=n)
            parts = [part.strip() for part in rest.split("|")]
            if not 3 <= len(parts) <= 7 or not all(parts) or any(len(p) > 200 for p in parts):
                raise self._fail(
                    "poll takes a question and two to six options, with | between them"
                )
            return Step("poll", text=parts[0], options=parts[1:], line_no=n)

        if verb == "mic":
            words = rest.split(maxsplit=1)
            if ident or not words or words[0] not in MIC_MODES:
                raise self._fail(f"mic takes one of {', '.join(MIC_MODES)}")
            return Step("mic", style=words[0], text=words[1] if len(words) > 1 else "", line_no=n)

        if verb == "scroll":
            try:
                amount = int(rest)
            except ValueError:
                raise self._fail("scroll takes a number of pixels") from None
            if ident or not -20000 <= amount <= 20000:
                raise self._fail("scroll takes a number of pixels")
            return Step("scroll", amount=amount, line_no=n)

        if verb == "pause":
            words = rest.split(maxsplit=1)
            try:
                seconds = float(words[0])
            except (IndexError, ValueError):
                raise self._fail("pause needs a number of seconds") from None
            if not 0 < seconds <= 600:
                raise self._fail("pause must be between 0 and 600 seconds")
            return Step(
                "pause", seconds=seconds, text=words[1] if len(words) > 1 else "", line_no=n
            )

        raise self._fail(f"unknown step {verb!r}")


def parse_steps(text: str, *, strict: bool = True) -> list[Step]:
    parser = StepParser(strict=strict)
    return [s for line in text.splitlines() if (s := parser.feed(line))]
