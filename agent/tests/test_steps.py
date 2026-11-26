import os

import pytest

from lecturer.room.board import Board, board_events
from lecturer.script.lesson import load_segment
from lecturer.script.steps import StepError, parse_steps
from lecturer.speech.decider import utterances_from_steps


def test_board_steps_ride_on_the_sentence_before_them():
    steps = parse_steps(
        """
        title: Outside and inside
        say: Let's call this function g.
        write g: g(x) = (\\part{inner}{3x+1})^5
        say: First, take x and work out the inside. Then raise the result to the fifth.
        mark: highlight g.inner
        pause: 5 Think.
        title: Next
        say: On we go.
        """
    )
    utts = utterances_from_steps(steps, "seg")
    shape = [(u.text, [s.verb for s in u.board], u.pause) for u in utts]
    assert shape == [
        ("", ["title"], 0),
        ("Let's call this function g.", ["write"], 0),
        ("First, take x and work out the inside.", [], 0),
        ("Then raise the result to the fifth.", ["mark"], 0),
        ("", [], 5),
        ("", ["title"], 0),  # a new page never rides on the sentence before it
        ("On we go.", [], 0),
    ]


def test_bad_scripts_are_refused():
    for bad in (
        "mark: highlight nothing",  # no such line
        "write a: x\nwrite a: y",  # id used twice
        "write a: x\ntitle: New page\nmark: box a",  # the title cleared it
        "shout: hello",
        "mark: sparkle a",
        "pause: soon",
    ):
        with pytest.raises(StepError):
            parse_steps(bad)


def test_a_live_stream_skips_bad_lines():
    steps = parse_steps("say: One.\nnonsense here\nsay: Two.", strict=False)
    assert [s.text for s in steps] == ["One.", "Two."]


def test_board_state_follows_events():
    board = Board()
    for step in parse_steps(
        "title: T\nwrite a: x\nwrite b: y\nmark: highlight a.inner\nmark: box b\nerase: b\nmark: clear a.inner"
    ):
        for ev in board_events(step):
            board.apply(ev)
    snap = board.snapshot()
    assert snap["page"] == 1
    assert [(ln["id"], ln["marks"]) for ln in snap["lines"]] == [("_title", {}), ("a", {})]


def test_a_segment_loads_from_a_lessons_folder(tmp_path, monkeypatch):
    course = tmp_path / "a-course"
    course.mkdir()
    (course / "a-lesson.seg").write_text(
        "---\nid: a-lesson\ncourse: a-course\ntitle: A lesson\n---\n"
        "title: A lesson\nsay: One.\nwrite a: x\nsay: Two.\n"
    )
    # The first folder listed that has the file wins; an empty one is passed over.
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("CH_LESSONS_DIR", f"{empty}{os.pathsep}{tmp_path}")
    seg = load_segment("a-course/a-lesson")
    assert seg.cover == "must"
    assert sum(1 for s in seg.steps if s.verb == "say") == 2


def test_a_wiped_page_is_kept_and_can_come_back():
    steps = parse_steps(
        """
        title: First
        write a: x
        title: Second
        write b: y
        page: First
        mark: box a
        write c: z
        page: Second
        mark: box b
        """
    )
    board = Board()
    seen = []
    for step in steps:
        for ev in board_events(step):
            board.apply(ev)
        seen.append((board.page, [ln.id for ln in board.lines]))
    assert seen[4] == (1, ["_title", "a"])  # back on the first page, nothing rewritten
    assert seen[-1] == (2, ["_title", "b"])
    assert [ln.id for ln in board.pages[1]] == ["_title", "a", "c"]
    again = Board.from_snapshot(board.snapshot())
    assert again.snapshot() == board.snapshot()

    with pytest.raises(StepError):
        parse_steps("title: First\npage: Never written")
    with pytest.raises(StepError):
        parse_steps("title: First\nwrite a: x\ntitle: Second\nmark: box a")


def test_gaps_and_a_second_column():
    steps = parse_steps(
        """
        title: T
        write a: 1
        gap:
        write b: 2
        column: right beside a
        note n: beside a
        note m: under n
        column: left
        write c: 3
        """
    )
    lines = {s.id: (s.col, s.beside, s.space) for s in steps if s.verb in ("write", "note")}
    assert lines == {
        "a": (0, "", False),
        "b": (0, "", True),  # the gap is used once
        "n": (1, "a", False),  # only the first line of the column names its level
        "m": (1, "", False),
        "c": (0, "", False),
    }
    board = Board()
    for step in steps:
        for ev in board_events(step):
            board.apply(ev)
    assert Board.from_snapshot(board.snapshot()).snapshot() == board.snapshot()
    assert [(ln.id, ln.col) for ln in board.lines if ln.col] == [("n", 1), ("m", 1)]

    with pytest.raises(StepError):
        parse_steps("title: T\ncolumn: right beside nothing")
    # A new page starts in the first column again.
    again = parse_steps("title: T\ncolumn: right\ntitle: U\nwrite z: 1")
    assert again[-1].col == 0


def test_the_stage_splits_any_way_and_panes_can_be_fixed():
    steps = parse_steps(
        """
        title: Rates
        title: Now
        show: slides 2 | board Rates | board / screen
        prepare: https://en.wikipedia.org/wiki/Derivative
        tab: 2
        """
    )
    assert steps[2].rows == [
        [{"source": "slides", "slide": 2}, {"source": "board", "page": 1}, {"source": "board"}],
        [{"source": "screen"}],
    ]
    assert [s.verb for s in steps[3:]] == ["prepare", "tab"]
    for bad in (
        "show: screen | screen",  # one video
        "show: board Nowhere",  # no such page
        "show: slides two",
        "show: board |",
        "tab: 0",
        "prepare: ftp://x",
    ):
        with pytest.raises(StepError):
            parse_steps(bad)


def test_the_shared_screen_takes_the_shape_of_its_pane():
    from lecturer.room.screen import shape_for

    assert shape_for(1, 1)[:4] == (1280, 720, 1024, 576)  # alone: wide
    assert shape_for(2, 1)[:2] == (960, 1080)  # beside one thing: tall
    assert shape_for(3, 1)[:2] == (640, 1080)
    assert shape_for(1, 2)[:2] == (1280, 360)  # under a row: a wide strip
    for cols, rows in ((1, 1), (2, 1), (3, 1), (4, 2)):
        video_w, video_h, page_w, page_h, scale = shape_for(cols, rows)
        assert video_w % 2 == 0 and video_h % 2 == 0 and page_w <= video_w
        assert abs(page_w * scale - video_w) < 1 and abs(page_h * scale - video_h) < 2


def test_who_may_speak_state():
    from lecturer.room.floor import FloorState

    floor = FloorState()
    events = [
        {"type": "floor.hand", "identity": "a", "name": "Ada", "up": True},
        {"type": "floor.hand", "identity": "b", "name": "Ben", "up": True},
        {"type": "floor.hand", "identity": "a", "name": "Ada", "up": True},  # twice: once
        {"type": "floor.give", "identity": "b", "name": "Ben"},
    ]
    for ev in events:
        floor.apply(ev)
    assert [h["name"] for h in floor.hands] == ["Ada"] and floor.speaker["name"] == "Ben"
    assert FloorState.from_snapshot(floor.snapshot()).snapshot() == floor.snapshot()
    floor.apply({"type": "floor.take", "identity": "b"})
    floor.apply({"type": "floor.mode", "mode": "open", "prompt": "Thoughts?"})
    assert floor.speaker is None and floor.hands == [] and floor.prompt == "Thoughts?"

    steps = parse_steps("mic: open What do you think?\nmic: hands\nmic: closed")
    assert [(s.style, s.text) for s in steps] == [
        ("open", "What do you think?"),
        ("hands", ""),
        ("closed", ""),
    ]
    with pytest.raises(StepError):
        parse_steps("mic: loud")


def test_polls():
    from lecturer.room.polls import PollState

    steps = parse_steps("poll: Which one? | This | That\npoll: close")
    assert (steps[0].text, steps[0].options, steps[1].style) == (
        "Which one?",
        ["This", "That"],
        "close",
    )
    for bad in ("poll: No options", "poll: One option | only", "poll: Blank | a | "):
        with pytest.raises(StepError):
            parse_steps(bad)

    state = PollState()
    state.apply({"type": "poll.open", "id": "p1", "question": "Q", "options": ["a", "b"]})
    state.apply({"type": "poll.results", "id": "p1", "counts": [2, 1]})
    state.apply({"type": "poll.open", "id": "p2", "question": "R", "options": ["c", "d"]})
    first, second = state.snapshot()
    assert (first["counts"], first["open"]) == ([2, 1], False)  # a new poll closes the old
    assert (second["counts"], second["open"]) == ([0, 0], True)
    state.apply({"type": "poll.close", "id": "p2", "counts": [0, 3]})
    assert state.snapshot()[1] == {
        "id": "p2",
        "question": "R",
        "options": ["c", "d"],
        "counts": [0, 3],
        "open": False,
    }


def test_a_part_name_must_be_a_plain_word():
    assert parse_steps("write g: (\\part{inner}{3x+1})^5")[0].id == "g"
    with pytest.raises(StepError):
        parse_steps("write g: \\part{a,b c=1}{x}")


def test_a_spoken_name_cannot_be_a_sentence_or_a_command():
    from lecturer.room.floor import spoken_name

    assert spoken_name("Ada") == "Ada"
    assert spoken_name("  Mary-Jane O'Neil ") == "Mary-Jane O'Neil"
    assert spoken_name("[[slnc 600000]] Bob") == "slnc 600000 Bob"
    assert len(spoken_name("Class is cancelled, everyone leave now please")) <= 24
    assert spoken_name("!!!") == ""


def test_the_log_keeps_what_happened_without_who():
    from lecturer.room.events import for_the_log

    hand = {"type": "floor.hand", "identity": "Ada__x1", "name": "Ada", "up": True, "seq": 3}
    assert for_the_log(hand) == {"type": "floor.hand", "up": True, "seq": 3}
    line = {"type": "say", "source": "invite", "text": "Ada, go ahead.", "id": "u"}
    assert "Ada" not in for_the_log(line)["text"]
    shown = {"type": "stage.show", "rows": [[{"source": "share", "who": "H__1", "name": "Hana"}]]}
    assert for_the_log(shown)["rows"] == [[{"source": "share"}]]
    lesson = {"type": "say", "source": "script", "text": "Ada Lovelace wrote Note G."}
    assert for_the_log(lesson) == lesson  # the lesson's own words are kept


def test_an_answer_from_a_model_can_only_speak_and_write():
    from lecturer.teaching.questions import answer_steps

    text = """Here is my answer.
say: You asked why the rates multiply.
write: \\frac{dy}{dx} = 5 \\cdot 3
mic: open
show: screen
open: https://example.org
poll: Hacked? | yes | no
say: Each step scales the one before it.
write: x = 1
say: So they multiply."""
    steps = answer_steps(text, "q1", board=True, says=5, writes=1)
    assert [s.verb for s in steps] == ["say", "write", "say", "say"]
    assert steps[1].id == "q1_1" and steps[1].space  # its own name, set apart on the board
    # With the board off the stage, the answer is spoken only.
    assert [s.verb for s in answer_steps(text, "q2", board=False, says=5, writes=1)] == ["say"] * 3
    # Nothing to say means no answer at all.
    assert answer_steps("write: x = 1", "q3", board=True, says=5, writes=1) == []
    assert answer_steps("", "q4", board=True, says=5, writes=1) == []


def test_a_log_note_can_have_a_field_called_kind(tmp_path):
    """Twice a note with a `kind` field crashed, because the note's own name was `kind`."""
    import json
    from types import SimpleNamespace

    from lecturer.room.events import ClassEvents

    events = ClassEvents(SimpleNamespace(), log_path=tmp_path / "log.jsonl")  # type: ignore[arg-type]
    events.note("question", kind="simple", what="a field called what", label_ms=3)
    events.close()
    line = json.loads((tmp_path / "log.jsonl").read_text())
    assert line["type"] == "note.question" and line["kind"] == "simple" and line["what"]


def test_every_step_the_writer_is_told_of_is_one_the_parser_knows() -> None:
    """The writer's prompt lists the steps it may write. Each must be a step the
    script parser knows (or one of the lines the teacher reads itself)."""
    import re
    from pathlib import Path

    from lecturer.script.steps import StepParser

    prompt = Path(__file__).resolve().parents[1] / "src/lecturer/teaching/prompts/writer.md"
    told = prompt.read_text().split("# What you write", 1)[1]
    verbs = set(re.findall(r"^    ([a-z]+)\b[^:\n]*:", told, re.MULTILINE))
    teachers_own = {"at", "why", "work", "aside", "kept", "expect", "end", "replan", "part"}
    assert {"say", "write", "pause", "split"} <= verbs  # the list was found
    for verb in sorted(verbs - teachers_own):
        try:
            StepParser().feed(f"{verb}: x")
        except StepError as e:
            assert "unknown step" not in str(e), f"the writer is told of `{verb}:`"


def test_the_lesson_check_passes_the_example_and_names_what_is_wrong(tmp_path, monkeypatch):
    from lecturer.check import check

    assert not [t for kind, t in check("example/cone") if kind == "problem"]
    course = tmp_path / "x"
    course.mkdir()
    (course / "bad.seg").write_text(
        "---\nid: bad\ncourse: x\ntitle: Bad\nminutes: 5\n---\n"
        "part: 1 | One | must | 5\nsay: Hello.\nslides: x/nope\nimage: missing.png\n"
    )
    monkeypatch.setenv("CH_LESSONS_DIR", str(tmp_path))
    problems = [t for kind, t in check("x/bad") if kind == "problem"]
    assert any("no x/nope.deck" in t for t in problems)
    assert any("missing.png is not there" in t for t in problems)
