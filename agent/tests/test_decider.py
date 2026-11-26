"""The voice decider, with a pretend voice and room."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fakes import PretendVoice

from lecturer.room.board import Board
from lecturer.room.stage import Stage
from lecturer.script.steps import parse_steps
from lecturer.speech.decider import Utterance, VoiceDecider, utterances_from_steps
from lecturer.speech.voice import SilentVoice


@dataclass
class FakeEvents:
    board: Board = field(default_factory=Board)
    stage: Stage = field(default_factory=Stage)
    sent: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    async def emit(self, *events: dict[str, Any]) -> None:
        for ev in events:
            self.sent.append(ev)
            if ev["type"].startswith("board."):
                self.board.apply(ev)

    def note(self, kind: str, **_: Any) -> None:
        self.notes.append(kind)

    def kinds(self) -> list[str]:
        return [e["type"] for e in self.sent if e["type"] not in ("plan",)]


def make(script: str, **kw: Any) -> tuple[VoiceDecider, PretendVoice, FakeEvents]:
    voice, events = PretendVoice(0.05, **kw), FakeEvents()
    decider = VoiceDecider(voice, events, gap_ms=0)  # type: ignore[arg-type]
    decider.enqueue(*utterances_from_steps(parse_steps(script), "seg"))
    return decider, voice, events


async def run(decider: VoiceDecider, during: Any = None) -> None:
    task = asyncio.create_task(decider.run())
    if during:
        await during()
    await asyncio.wait_for(decider.wait_until_idle(), timeout=5)
    task.cancel()


async def test_a_held_sentence_is_said_again_and_its_writing_is_not_repeated():
    decider, _, events = make("say: The first sentence here.\nwrite a: x = 1")

    async def during() -> None:
        await asyncio.sleep(0.02)  # mid-sentence
        decider.hold("empty room")
        await asyncio.sleep(0.05)
        assert "cut" in events.kinds()
        decider.resume("empty room")

    await run(decider, during)
    assert events.kinds() == ["say", "board.write", "cut", "say", "heard"]
    assert [ln.id for ln in events.board.lines] == ["a"]


async def test_two_holds_need_two_resumes():
    decider, _, events = make("say: The first sentence here.")
    decider.hold("empty room")
    decider.hold("floor")

    async def during() -> None:
        decider.resume("empty room")
        await asyncio.sleep(0.05)
        assert events.kinds() == []  # the floor hold still stands
        decider.resume("floor")

    await run(decider, during)
    assert events.kinds() == ["say", "heard"]


async def test_a_sentence_without_a_voice_is_tried_again_then_shown_as_a_caption():
    text = "This sentence has no voice."
    decider, voice, events = make(f"say: {text}\nwrite a: x = 1", broken={text})
    await run(decider)
    assert voice.prepared.count(text) == 2  # one retry
    assert events.kinds() == ["say", "board.write", "cut"]  # still shown, never "heard"
    assert "voice_failed" in events.notes


async def test_a_thinking_pause_keeps_its_time_through_a_hold():
    decider, _, events = make("pause: 0.3 Think.")

    async def during() -> None:
        await asyncio.sleep(0.1)
        decider.hold("empty room")
        await asyncio.sleep(0.3)  # longer than the pause itself
        assert "pause.end" not in events.kinds()  # the hold did not eat it
        decider.resume("empty room")

    await run(decider, during)
    pauses = [e["seconds"] for e in events.sent if e["type"] == "pause"]
    assert len(pauses) == 2 and pauses[0] == 0.3 and 0.1 <= pauses[1] <= 0.25
    assert events.kinds()[-1] == "pause.end"


async def test_a_sentence_cut_by_a_hold_that_ends_at_once_is_still_said_again():
    # Seen in a real class: the only student left and was back within 2 ms, as the
    # last sentence began. It was cut and never said again.
    decider, _, events = make("say: The very last sentence.")

    async def during() -> None:
        await asyncio.sleep(0.02)
        decider.hold("empty room")
        decider.resume("empty room")

    await run(decider, during)
    assert events.kinds() == ["say", "cut", "say", "heard"]


async def test_a_ready_line_is_said_next_and_can_start_a_hold():
    decider, _, events = make("say: The first sentence here.\nsay: The second sentence here.")
    said: list[str] = []

    async def during() -> None:
        await asyncio.sleep(0.02)  # during the first sentence
        await decider.interject("Ada, go ahead.", then_hold="floor")
        said.extend(e["text"] for e in events.sent if e["type"] == "say")
        await asyncio.sleep(0.15)
        # The lesson has not gone on: the floor hold began as the line ended.
        assert [e["text"] for e in events.sent if e["type"] == "say"] == said
        decider.resume("floor")

    await run(decider, during)
    assert said == ["The first sentence here.", "Ada, go ahead."]
    texts = [e["text"] for e in events.sent if e["type"] == "say"]
    assert texts[-1] == "The second sentence here."


async def test_a_reaction_is_noted_and_done_from_everyone_ends_the_pause():
    """The reaction code against the real event log's note(), which once clashed."""
    from pathlib import Path
    from types import SimpleNamespace

    from lecturer.room.events import ClassEvents
    from lecturer.room.react import Reactions

    student = SimpleNamespace(identity="a", kind=0, attributes={})
    room = SimpleNamespace(remote_participants={"a": student}, local_participant=None)
    events = ClassEvents(room, log_path=Path("/dev/null"))  # type: ignore[arg-type]
    ended: list[bool] = []
    decider = SimpleNamespace(end_pause=lambda: ended.append(True))
    reactions = Reactions(room, events, decider)  # type: ignore[arg-type]

    class Stream:
        """Text arriving in pieces, as LiveKit hands it over."""

        def __init__(self, *pieces: str) -> None:
            self.pieces, self.closed = list(pieces), False

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> str:
            if not self.pieces:
                raise StopAsyncIteration
            return self.pieces.pop(0)

        def close(self) -> None:
            self.closed = True

    await reactions._read(Stream("do", "ne"), "a")  # type: ignore[arg-type]
    assert reactions.recent("done", 5) == 1 and ended == [True]

    # A stream far longer than any reaction is dropped part-way and closed.
    huge = Stream(*["x" * 1000] * 50)
    await reactions._read(huge, "a")  # type: ignore[arg-type]
    assert huge.closed and len(huge.pieces) == 49
    events.close()


async def test_the_voice_survives_its_queue_being_emptied_during_a_hold() -> None:
    """Lines taken back while the voice is held must not stop it for good."""
    voice, events = PretendVoice(0.05), FakeEvents()
    d = VoiceDecider(voice, events, gap_ms=0)  # type: ignore[arg-type]
    d.hold("floor")
    d.enqueue(Utterance(text="One two three four five.", source="writer"))
    task = asyncio.create_task(d.run())
    await asyncio.sleep(0.05)  # waiting for the hold, with a line queued
    d.drop_unspoken("writer")
    d.resume("floor")
    await asyncio.sleep(0.1)
    assert not task.done()
    d.enqueue(Utterance(text="A later sentence.", source="writer"))
    await asyncio.sleep(0.3)
    assert any(e["type"] == "say" for e in events.sent)
    task.cancel()


async def test_with_no_voice_a_sentence_still_takes_its_time() -> None:
    voice = SilentVoice()
    clip = voice.prepare("one two three four five six seven eight nine ten eleven twelve thirteen")
    assert clip.ready and 4000 < clip.duration_ms < 6000
    clip.duration_ms = 200  # quicker, for the test
    started: list[bool] = []

    async def on_start() -> None:
        started.append(True)

    played = await voice.play(clip, on_start)
    assert started and played.completed
    stopping = asyncio.create_task(voice.play(voice.prepare("a long sentence " * 20)))
    await asyncio.sleep(0.05)
    voice.stop()
    assert not (await stopping).completed
