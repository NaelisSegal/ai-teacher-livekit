"""Slide decks: what lesson prep hands the lecturer to show.

A deck file sits beside the lesson scripts, as `<course>/<deck>.deck`.
Slides are a fixed 16:9 shape and are drawn in each browser, not sent as pictures.

    ---
    id: chain-rule
    title: The chain rule
    ---
    slide: Two jobs, one after the other
    > A composition is one function fed into another.
    - (inside) The inside job turns x into u.
    - (outside) The outside job turns u into y.
    $ (comp) y = f(g(x))
    image: chain-rule-machines.svg
    area work: 0.55 0.55 0.4 0.35

Lines in a slide:
    > text            a paragraph
    - text            a bullet
    $ LaTeX           an equation
    image: <file>     a picture, from the same folder as the deck
    video: <file | address>    a video: a file from that folder, or a YouTube or Twitch
                               address. The lecturer plays it with `play:`
    area <name>: x y w h    an empty area the lecturer can write in, as fractions
                            of the slide's width and height

`(name)` after the marker names an item so the lecturer can mark it later with
`mark: highlight slide.name`. Areas can be marked by name too.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from .lesson import lesson_file
from .media import video_ref

_NAME = re.compile(r"^\((?P<name>[A-Za-z][\w-]*)\)\s*(?P<rest>.*)$")
_AREA = re.compile(r"^area\s+(?P<name>[A-Za-z][\w-]*)\s*:\s*(?P<box>.+)$")
_IMAGE = re.compile(r"^[\w][\w.-]*\.(svg|png|jpg|jpeg|webp)$", re.IGNORECASE)
KINDS = {">": "text", "-": "bullet", "$": "math"}


class DeckError(ValueError):
    pass


def parse_deck(text: str, folder: str) -> dict[str, Any]:
    """Parse a deck. `folder` is its folder under lessons/, used for picture paths."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise DeckError("a deck starts with a --- header")
    try:
        end = next(i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---")
    except StopIteration:
        raise DeckError("the deck header is not closed with ---") from None
    head = {}
    for ln in lines[1:end]:
        if ln.strip():
            key, _, value = ln.partition(":")
            head[key.strip()] = value.strip()
    if not head.get("id"):
        raise DeckError("the deck header needs an id")

    slides: list[dict[str, Any]] = []
    for number, raw in enumerate(lines[end + 1 :], end + 2):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        def fail(msg: str, number: int = number) -> DeckError:
            return DeckError(f"line {number}: {msg}")

        if line.startswith("slide:"):
            slides.append({"title": line[6:].strip(), "items": [], "areas": {}})
            continue
        if not slides:
            raise fail("content before the first `slide:`")
        slide = slides[-1]
        if m := _AREA.match(line):
            try:
                box = [float(v) for v in m["box"].split()]
            except ValueError:
                raise fail("an area is four numbers: x y w h") from None
            if len(box) != 4 or not all(0 <= v <= 1 for v in box):
                raise fail("an area is four numbers between 0 and 1: x y w h")
            if box[0] + box[2] > 1.001 or box[1] + box[3] > 1.001:
                raise fail("the area goes off the slide")
            slide["areas"][m["name"]] = box
        elif line.startswith("image:"):
            file = line[6:].strip()
            if not _IMAGE.match(file):
                raise fail("image takes a picture file name from the deck's folder")
            slide["items"].append({"kind": "image", "name": "", "content": f"{folder}/{file}"})
        elif line.startswith("video:"):
            ref = video_ref(line[6:].strip(), folder)
            if ref is None:
                raise fail("video takes a video file, or a YouTube or Twitch address")
            slide["items"].append(
                {"kind": "video", "name": "", "content": ref.id, "video": ref.to_json()}
            )
        elif line[0] in KINDS and line[1:2] in ("", " "):
            body = line[1:].strip()
            name = ""
            if m := _NAME.match(body):
                name, body = m["name"], m["rest"]
            if not body:
                raise fail("this line has no content")
            slide["items"].append({"kind": KINDS[line[0]], "name": name, "content": body})
        else:
            raise fail(f"not a slide line: {line!r}")
    if not slides:
        raise DeckError("the deck has no slides")
    return {"id": head["id"], "title": head.get("title", ""), "slides": slides}


@lru_cache(maxsize=32)
def load_deck(name: str) -> dict[str, Any]:
    """Load the deck `<name>.deck`, where the name is `<course>/<deck>`."""
    return parse_deck(lesson_file(f"{name}.deck").read_text(), name.split("/")[0])


if __name__ == "__main__":
    # Prints a deck as the browser receives it:
    # `python -m lecturer.script.deck <course>/<deck>`.
    import json
    import sys

    if len(sys.argv) != 2 or not re.fullmatch(r"[a-z0-9-]+/[a-z0-9-]+", sys.argv[1]):
        sys.exit("usage: python -m lecturer.script.deck <course>/<deck>")
    print(json.dumps(load_deck(sys.argv[1])))
