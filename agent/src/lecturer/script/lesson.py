"""Lesson segments: what lesson prep hands to the lecturer.

A segment file is a short header and then a full speakable script as steps (see
`steps.py`). The script is the safe default: if nothing else is ready, it is read
as written.

    ---
    id: my-lesson
    course: my-course
    title: My lesson
    cover: must
    minutes: 6
    ---
    title: The chain rule
    say: ...

A longer lesson is divided into parts, so that the live lecturer knows where it is,
what is still to come, and what may be cut:

    part: 6b | Both directions | must | 3

A part line gives the part's id, its title, `must` or `optional`, and about how many
minutes its script takes. The steps after it, up to the next part line, are that part.

Beside the script there may be teaching notes (`<name>.notes.md`): what a teacher
knows besides the script. `load_prep` gathers all of it for the live lecturer.

Nothing here names a lesson. A lesson is found by its name, `<course>/<lesson>`, in
the lessons folders: `lessons/` at the top of the repo, or the folders listed in
CH_LESSONS_DIR (several are separated by `:`, and the first that has the file wins).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .steps import Step, StepParser

DEFAULT_LESSONS_DIR = Path(__file__).resolve().parents[4] / "lessons"


def lesson_dirs() -> list[Path]:
    """The folders lessons are looked for in, in order."""
    listed = [d for d in os.environ.get("CH_LESSONS_DIR", "").split(os.pathsep) if d]
    return [Path(d).resolve() for d in listed] or [DEFAULT_LESSONS_DIR]


def lesson_file(name: str) -> Path:
    """Where `<course>/<file>` is. If no folder has it, where it was looked for first."""
    dirs = lesson_dirs()
    return next((d / name for d in dirs if (d / name).is_file()), dirs[0] / name)


@dataclass
class Part:
    id: str
    title: str
    cover: str  # "must" or "optional"
    minutes: float
    lines: list[str] = field(default_factory=list)  # its script, as written


@dataclass
class Segment:
    id: str
    course: str
    title: str
    cover: str  # "must" or "optional"
    minutes: float
    source: str
    steps: list[Step]
    parts: list[Part] = field(default_factory=list)  # empty if the script has no part lines


@dataclass
class Prep:
    """Everything lesson prep hands the live lecturer."""

    segment: Segment
    script: str  # the script as written, part lines and comments included
    notes: str  # the teaching notes, or "" if there are none
    deck: str  # the slide deck as written, or ""


def _parse_part(rest: str, line_no: int) -> Part:
    fields = [f.strip() for f in rest.split("|")]
    if len(fields) != 4 or not all(fields):
        raise ValueError(f"line {line_no}: a part is `part: id | title | must|optional | minutes`")
    part_id, title, cover, minutes = fields
    if cover not in ("must", "optional"):
        raise ValueError(f"line {line_no}: a part is must or optional")
    try:
        return Part(part_id, title, cover, float(minutes))
    except ValueError:
        raise ValueError(f"line {line_no}: a part's minutes must be a number") from None


def parse_segment(text: str) -> Segment:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("a segment starts with a --- header")
    try:
        end = next(i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---")
    except StopIteration:
        raise ValueError("the segment header is not closed with ---") from None
    head: dict[str, str] = {}
    for ln in lines[1:end]:
        if ln.strip():
            key, _, value = ln.partition(":")
            head[key.strip()] = value.strip()
    for key in ("id", "course", "title"):
        if not head.get(key):
            raise ValueError(f"the segment header needs {key}")
    cover = head.get("cover", "must")
    if cover not in ("must", "optional"):
        raise ValueError("cover is must or optional")
    # Fed a line at a time, with the header counted, so that error messages give the
    # file's own line numbers and one parser sees the whole script.
    parser = StepParser()
    for _ in range(end + 1):
        parser.feed("")
    steps: list[Step] = []
    parts: list[Part] = []
    for number, line in enumerate(lines[end + 1 :], end + 2):
        if line.strip().startswith("part:"):
            part = _parse_part(line.strip()[5:], number)
            if any(p.id == part.id for p in parts):
                raise ValueError(f"line {number}: there are two parts called {part.id!r}")
            parts.append(part)
            parser.feed("")
            continue
        if step := parser.feed(line):
            steps.append(step)
        if parts and line.strip() and not line.strip().startswith("#"):
            parts[-1].lines.append(line.strip())
    return Segment(
        id=head["id"],
        course=head["course"],
        title=head["title"],
        cover=cover,
        minutes=float(head.get("minutes", 0) or 0),
        source=head.get("source", ""),
        steps=steps,
        parts=parts,
    )


def load_segment(name: str) -> Segment:
    """Load the script `<name>.seg`, where the name is `<course>/<lesson>`."""
    return parse_segment(lesson_file(f"{name}.seg").read_text())


def load_prep(name: str) -> Prep:
    """Load a lesson's script, its notes and its deck, for the live lecturer."""
    script = lesson_file(f"{name}.seg").read_text()
    segment = parse_segment(script)
    notes = lesson_file(f"{name}.notes.md")
    # The deck is whichever one the script loads first.
    deck = next((s.text for s in segment.steps if s.verb == "slides"), "")
    deck_file = lesson_file(f"{deck}.deck") if deck else None
    return Prep(
        segment=segment,
        script=script,
        notes=notes.read_text() if notes.exists() else "",
        deck=deck_file.read_text() if deck_file and deck_file.exists() else "",
    )
