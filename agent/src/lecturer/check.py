"""Check a lesson before teaching it.

    cd agent && uv run python -m lecturer.check <course>/<lesson> [<course>/<lesson> ...]

It reads the lesson the way the lecturer does, without models or servers, and says:

    - whether the script reads, and if not, which line and how to write it
    - its parts, their minutes, and about how long it takes read aloud, against its
      time slot (the header's `minutes`)
    - whether it can be taught live (it has parts; teaching notes help)
    - whether the slides it loads are there, and every slide item it marks
    - whether the pictures and video files it uses are there
    - the web videos in its prep, which are the ones a live teacher may play

The exit code is 1 if anything is wrong, so it can run in CI. Lessons are found as the
lecturer finds them: in lessons/, or the folders in CH_LESSONS_DIR.
"""

from __future__ import annotations

import sys

from .script.deck import DeckError, parse_deck
from .script.lesson import lesson_file, load_prep
from .script.media import picture_file, video_ref, videos_in
from .script.steps import StepError

WORDS_PER_SECOND = 2.6  # about how fast a sentence is said


def check(name: str) -> list[tuple[str, str]]:
    """What is right and wrong with a lesson, as (kind, text): ok, note or problem."""
    out: list[tuple[str, str]] = []
    script = lesson_file(f"{name}.seg")
    if not script.is_file():
        return [("problem", f"no script at {script} (a lesson is <course>/<lesson>.seg)")]
    try:
        prep = load_prep(name)
    except (StepError, ValueError) as e:
        return [("problem", f"the script does not read: {e}")]
    seg = prep.segment
    folder = name.split("/")[0]
    out.append(("ok", f'the script reads: {len(seg.steps)} steps, "{seg.title}"'))

    # Time.
    words = sum(len(s.text.split()) for s in seg.steps if s.verb == "say")
    waits = sum(s.seconds for s in seg.steps if s.verb == "pause")
    waits += sum(s.seconds for s in seg.steps if s.verb == "play" and s.seconds > 0)
    read = (words / WORDS_PER_SECOND + waits) / 60
    slot = f", in a slot of {seg.minutes:g}" if seg.minutes else " (no `minutes` in the header)"
    out.append(("ok", f"read aloud with its waits, about {read:.0f} minutes{slot}"))
    if seg.minutes and read > seg.minutes:
        out.append(("note", "it takes longer than its slot: live, optional parts will be cut"))

    # Live.
    if seg.parts:
        total = sum(p.minutes for p in seg.parts)
        out.append(
            ("ok", f"{len(seg.parts)} parts, {total:g} minutes between them: can be taught live")
        )
        if seg.minutes and total > seg.minutes:
            out.append(("note", f"the parts add up to more than the slot ({seg.minutes:g})"))
    else:
        out.append(
            ("note", "no `part:` lines: it can only be read from the script, not taught live")
        )
    if prep.notes:
        out.append(("ok", "teaching notes are there"))
    elif seg.parts:
        out.append(
            ("note", f"no teaching notes ({name}.notes.md): a live teacher has only the script")
        )

    # Slides.
    names: set[str] = set()
    for deck in sorted({s.text for s in seg.steps if s.verb == "slides"}):
        path = lesson_file(f"{deck}.deck")
        if not path.is_file():
            out.append(("problem", f"slides `{deck}` are loaded, but there is no {deck}.deck"))
            continue
        try:
            parsed = parse_deck(path.read_text(), deck.split("/")[0])
        except DeckError as e:
            out.append(("problem", f"the slides {deck}.deck do not read: {e}"))
            continue
        out.append(("ok", f"the slides {deck}.deck read: {len(parsed['slides'])} slides"))
        for slide in parsed["slides"]:
            names |= {i["name"] for i in slide["items"] if i["name"]} | set(slide["areas"])
            for item in slide["items"]:
                if item["kind"] == "image":
                    out += file_there(item["content"], "picture")
                elif item["kind"] == "video" and item["video"]["kind"] == "file":
                    out += file_there(item["video"]["id"], "video")
    for step in seg.steps:
        # `mark: <style> slide.<name>`: the name is the part after `slide.`.
        if step.on == "slide" and step.verb == "mark" and step.part and step.part not in names:
            out.append(
                ("problem", f"line {step.line_no}: no slide item or area called {step.part!r}")
            )
        if step.on == "slide" and step.area and step.area not in names:
            out.append(("problem", f"line {step.line_no}: no slide area called {step.area!r}"))

    # Pictures and video files.
    for step in seg.steps:
        if step.verb == "image":
            found = picture_file(step.text, folder)
            if found:
                out += file_there(found, "picture", step.line_no)
        elif step.verb == "video":
            ref = video_ref(step.text, folder)
            if ref is not None and ref.kind == "file":
                out += file_there(ref.id, "video", step.line_no)

    # Web videos a live teacher may play.
    web = {
        ref.key: address
        for address, ref in videos_in(f"{prep.script}\n{prep.notes}\n{prep.deck}", most=200)
    }
    if web:
        out.append(("ok", f"{len(web)} web video(s) in the prep, which a live teacher may play"))
    return out


def file_there(name: str, what: str, line: int = 0) -> list[tuple[str, str]]:
    where = f"line {line}: " if line else ""
    if lesson_file(name).is_file():
        return []
    return [("problem", f"{where}the {what} {name} is not there")]


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip().splitlines()[2].strip())
        return 2
    wrong = False
    for name in argv:
        print(name)
        for kind, text in check(name.removesuffix(".seg")):
            mark = {"ok": "  ok     ", "note": "  note   ", "problem": "  PROBLEM"}[kind]
            print(f"{mark} {text}")
            wrong = wrong or kind == "problem"
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
