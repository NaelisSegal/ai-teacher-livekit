"""The live teacher's loop, with a pretend writer, voice and room. No models are called."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fakes import PretendRoom, PretendVoice, until

from lecturer.models.writer_planner import ModelError, Use
from lecturer.room.events import ClassEvents
from lecturer.script.lesson import Prep, parse_segment
from lecturer.speech.decider import VoiceDecider
from lecturer.teaching.teacher import Teacher

SCRIPT = """---
id: test
course: test
title: A test lesson
minutes: 5
---
part: 1 | Start | must | 1
say: This is the script's first part.
part: 2 | End | must | 1
say: This is the script's second part.
"""


class _Writer:
    """Gives each turn's lines from a list, and records what it was shown."""

    def __init__(self, turns: list[list[str] | Exception]) -> None:
        self.turns = turns
        self.shown: list[str] = []
        self.use = Use()

    async def lines(self, _system: str, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        self.shown.append(messages[-1]["content"])
        turn = self.turns.pop(0) if self.turns else ["end:"]
        if isinstance(turn, Exception):
            raise turn
        for line in turn:
            await asyncio.sleep(0)
            yield line


class _Decisions:
    """Every chat message is for the teacher, or (during a task) an answer."""

    def __init__(self) -> None:
        self.calls = 0

    async def ask_many(self, questions: list[str]) -> list[float | None]:
        self.calls += 1
        during_task = len(questions) == 3
        if len(questions) == 1:  # is the answer right?
            return [1.0]
        return [0.1, 0.0, 0.9] if during_task else [0.9, 0.0]


def make(
    turns: list[list[str] | Exception], names: dict[str, str] | None = None
) -> tuple[Teacher, _Writer, VoiceDecider]:
    events = ClassEvents(PretendRoom())  # type: ignore[arg-type]
    decider = VoiceDecider(PretendVoice(), events, gap_ms=0, pause_scale=0.1)  # type: ignore[arg-type]
    writer = _Writer(turns)
    teacher = Teacher(
        Prep(parse_segment(SCRIPT), SCRIPT, "", ""),
        decider,
        events,
        writer,  # type: ignore[arg-type]
        students=lambda: ["a", "b"],
        decisions=_Decisions(),
        time_scale=10,  # class time runs ten times faster, to match the voice
        name_of=names.get if names is not None else None,
    )
    return teacher, writer, decider


async def run(teacher: Teacher, decider: VoiceDecider, during: Any = None) -> None:
    voice = asyncio.create_task(decider.run())
    lesson = asyncio.create_task(teacher.run())
    if during:
        await during()
    await asyncio.wait_for(lesson, 10)
    voice.cancel()


async def test_a_pause_ends_the_turn_and_the_writer_sees_the_answers() -> None:
    teacher, writer, decider = make(
        [
            ["at: 1", "say: Write the partner of A T G.", "expect: TAC", "pause: 2 Try it"],
            ["say: Let's see.", "end:"],
        ]
    )

    async def during() -> None:
        await until(lambda: teacher._wait is not None)
        teacher.on_chat("a", "TAC")
        teacher.on_reaction("b", "done")

    await run(teacher, decider, during)
    assert len(writer.shown) == 2
    after = writer.shown[1]
    assert "The wait you set is over" in after
    assert 's1 "TAC" (right)' in after
    assert "1 of 2 pressed done" in after
    # Both students responded, so the wait ended early.
    assert "ended early" in after


async def test_a_message_for_the_teacher_takes_back_what_was_not_said() -> None:
    long = [f"say: Sentence number {n} of the lesson." for n in range(1, 30)]
    teacher, writer, decider = make([["at: 1", *long], ["say: To your question.", "end:"]])

    async def during() -> None:
        await until(lambda: "Sentence number 2 of the lesson." in teacher._said)
        teacher.on_chat("a", "what is a base?")

    await run(teacher, decider, during)
    respond = writer.shown[1]
    assert "Something has come in for you" in respond
    assert "taken back" in respond
    assert '"what is a base?" [for you]' in respond
    # Taken back means not said.
    assert "Sentence number 29 of the lesson." not in teacher._said


async def test_the_script_is_read_when_the_writer_fails() -> None:
    failure = ModelError("pretend failure")
    teacher, _writer, decider = make([failure, failure, ["end:"]])
    await run(teacher, decider)
    assert "This is the script's first part." in teacher._said


async def test_a_new_wait_is_not_ended_by_answers_to_the_last_one() -> None:
    """Everyone answered the first task. The wait set straight after it (a second task,
    or time to watch a video) must run, not end at once because "everyone has responded"."""
    teacher, _writer, decider = make(
        [
            ["at: 1", "say: Try this.", "expect: 4", "pause: 30 What is 2 + 2?"],
            ["say: Good. Now a second one.", "pause: 6 What is 3 + 3?"],
            ["say: That is all.", "end:"],
        ]
    )

    async def during() -> None:
        await until(lambda: teacher._wait is not None)
        teacher.on_chat("a", "4")
        teacher.on_reaction("b", "done")

    await run(teacher, decider, during)
    first, second = teacher._record
    assert first.early and not second.early
    assert second.waited >= 5
    # What they had typed is still shown with the second wait, as more time would want.
    assert "a" in second.answers and not second.since


async def test_a_class_record_is_written_as_it_happens(tmp_path: Any) -> None:
    events = ClassEvents(PretendRoom())  # type: ignore[arg-type]
    decider = VoiceDecider(PretendVoice(), events, gap_ms=0, pause_scale=0.1)  # type: ignore[arg-type]
    writer = _Writer([["at: 1", "say: Try it.", "pause: 2 What is 2 + 2?"], ["end:"]])
    record = tmp_path / "recorded" / "room.jsonl"
    teacher = Teacher(
        Prep(parse_segment(SCRIPT), SCRIPT, "", ""),
        decider,
        events,
        writer,  # type: ignore[arg-type]
        students=lambda: ["a"],
        decisions=_Decisions(),
        time_scale=10,
        record=record,
    )

    async def during() -> None:
        await until(lambda: teacher._wait is not None)
        teacher.on_chat("a", "4")

    await run(teacher, decider, during)
    kinds = [json.loads(line)["kind"] for line in record.read_text().splitlines()]
    assert "chat" in kinds and "wrote" in kinds
    assert any(json.loads(ln).get("text") == "4" for ln in record.read_text().splitlines())


async def test_an_answer_just_after_a_wait_is_still_an_answer() -> None:
    teacher, _writer, decider = make(
        [
            ["at: 1", "say: Try it.", "expect: 4", "pause: 2 What is 2 + 2?"],
            ["say: The answer is four, and here is why. " * 3, "say: And more.", "end:"],
        ]
    )

    async def during() -> None:
        await until(lambda: bool(teacher._record))  # the wait is over
        teacher.on_chat("a", "4")

    await run(teacher, decider, during)
    assert any("a typed" not in t and 's1 answered "4" (right)' in t for t in teacher._transcript)


class _NotYet(_Decisions):
    """Every typed answer is still on the way: none has reached the right answer."""

    async def ask_many(self, questions: list[str]) -> list[float | None]:
        if len(questions) == 1:
            return [0.0]
        return await super().ask_many(questions)


async def test_working_typed_in_the_chat_does_not_end_the_thinking_time() -> None:
    teacher, writer, decider = make(
        [
            ["at: 1", "say: Try it.", "expect: 16", "pause: 5 How many fit?"],
            ["say: Let's see.", "end:"],
        ]
    )
    teacher._decisions = _NotYet()

    async def during() -> None:
        await until(lambda: teacher._wait is not None)
        teacher.on_chat("a", "first I found the area, 12")
        teacher.on_chat("b", "the length is 3, then?")

    await run(teacher, decider, during)
    (wait,) = teacher._record
    assert not wait.early and wait.waited >= 4
    assert "not the right answer, yet" in writer.shown[1]


async def test_a_student_label_is_never_said_aloud() -> None:
    teacher, _writer, decider = make([["at: 1", "say: And s2, your setup was right.", "end:"]])
    await run(teacher, decider)
    assert "And one of you, your setup was right." in teacher._said


async def test_what_is_said_aloud_reaches_the_teacher_as_said() -> None:
    long = [f"say: Sentence number {n} of the lesson." for n in range(1, 30)]
    teacher, writer, decider = make([["at: 1", *long], ["say: To your question.", "end:"]])

    async def during() -> None:
        await until(lambda: "Sentence number 2 of the lesson." in teacher._said)
        teacher.on_speech("a", "why is it a third?")

    await run(teacher, decider, during)
    assert any('said aloud: "why is it a third?"' in shown for shown in writer.shown)


async def test_a_turn_with_nothing_heard_is_followed_up() -> None:
    long = [f"say: Sentence number {n} of the lesson." for n in range(1, 30)]
    teacher, writer, decider = make(
        [["at: 1", *long], ["say: Do you still have a question?", "end:"]]
    )

    async def during() -> None:
        await until(lambda: "Sentence number 2 of the lesson." in teacher._said)
        teacher.on_floor_turn("a", spoke=False, can_hear=True)

    await run(teacher, decider, during)
    assert any("nothing was heard" in shown and "still have a" in shown for shown in writer.shown)


def test_your_own_prompts_are_used_instead_of_ours(tmp_path: Any, monkeypatch: Any) -> None:
    from lecturer.teaching.teacher import prompt

    (tmp_path / "writer.md").write_text("Teach like this.")
    monkeypatch.setenv("CH_PROMPTS_DIR", str(tmp_path))
    assert prompt("writer.md") == "Teach like this."
    assert "You plan the rest of a live class" in prompt("planner.md")  # not in the folder


async def test_past_the_writer_limit_the_script_is_read() -> None:
    events = ClassEvents(PretendRoom())  # type: ignore[arg-type]
    decider = VoiceDecider(PretendVoice(), events, gap_ms=0, pause_scale=0.1)  # type: ignore[arg-type]
    writer = _Writer([["at: 1", "say: Written by the writer."]])
    teacher = Teacher(
        Prep(parse_segment(SCRIPT), SCRIPT, "", ""),
        decider,
        events,
        writer,  # type: ignore[arg-type]
        students=lambda: ["a"],
        decisions=_Decisions(),
        time_scale=10,
        writer_calls=1,
    )
    writer.use.calls = 0

    async def counted(system: str, messages: list[dict[str, Any]]) -> AsyncIterator[str]:
        writer.use.calls += 1
        async for line in _Writer.lines(writer, system, messages):
            yield line

    writer.lines = counted  # type: ignore[method-assign]
    await run(teacher, decider)
    assert "Written by the writer." in teacher._said
    assert "This is the script's second part." in teacher._said
    assert writer.use.calls == 1


async def test_only_pages_from_the_prep_or_the_chat_can_be_opened() -> None:
    teacher, writer, decider = make(
        [["at: 1", "say: Look at this.", "open: https://somewhere.example/page"], ["end:"]]
    )
    await run(teacher, decider)
    assert any(e["kind"] == "bad line" for e in teacher.trace)
    assert "was not opened" in writer.shown[-1]


async def test_with_names_on_the_writer_knows_students_by_name_and_label() -> None:
    long = [f"say: Sentence number {n} of the lesson." for n in range(1, 30)]
    teacher, writer, decider = make(
        [["at: 1", *long], ["say: Good question, Ada.", "end:"]], names={"a": "Ada"}
    )

    async def during() -> None:
        await until(lambda: "Sentence number 2 of the lesson." in teacher._said)
        teacher.on_chat("a", "why is it a third?")

    await run(teacher, decider, during)
    assert any("Ada (s1)" in shown and "why is it a third?" in shown for shown in writer.shown)
    assert "may use their names" in teacher._system
    assert "Good question, Ada." in teacher._said


async def test_with_labels_only_nobody_is_named() -> None:
    teacher, _writer, decider = make([["at: 1", "say: Hello.", "end:"]])
    await run(teacher, decider)
    assert "nobody is named in class" in teacher._system
    assert teacher._handle("a") == "s1"
