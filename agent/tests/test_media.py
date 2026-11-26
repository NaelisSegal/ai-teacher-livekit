"""Pictures and videos: how they are named, what the class is sent, and the image model."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from fakes import PretendRoom, PretendVoice, until
from test_teacher import make, run

from lecturer.models.images import ImageModel, picture_type
from lecturer.models.providers import ModelError
from lecturer.room.board import Board, board_events
from lecturer.room.events import ClassEvents
from lecturer.room.pictures import Pictures
from lecturer.room.stage import Stage, stage_events
from lecturer.script.deck import parse_deck
from lecturer.script.media import seconds, video_ref, videos_in
from lecturer.script.steps import StepError, parse_steps
from lecturer.speech.decider import WATCHING, VoiceDecider, utterances_from_steps

# The smallest thing that is a PNG as far as its first bytes say.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


# -- how pictures and videos are named ----------------------------------------------------


def test_a_video_is_a_file_or_a_site_we_can_show() -> None:
    assert video_ref("clip.mp4", "bio").id == "bio/clip.mp4"  # type: ignore[union-attr]
    assert video_ref("other/clip.webm").id == "other/clip.webm"  # type: ignore[union-attr]
    assert video_ref("clip.mp4") is None  # no folder is known for it
    watch = video_ref("https://www.youtube.com/watch?v=abcdefghijk&t=1m5s")
    assert watch is not None and (watch.kind, watch.id, watch.start) == (
        "youtube",
        "abcdefghijk",
        65,
    )
    assert video_ref("https://youtu.be/abcdefghijk").key == watch.key  # type: ignore[union-attr]
    vod = video_ref("https://www.twitch.tv/videos/123456?t=1h2m3s")
    assert vod is not None and (vod.form, vod.id, vod.start) == ("video", "123456", 3723)
    assert video_ref("https://clips.twitch.tv/Some-Clip_1").form == "clip"  # type: ignore[union-attr]
    assert video_ref("https://www.twitch.tv/somebody").form == "channel"  # type: ignore[union-attr]
    # Not a site whose player we put in the page, and not a video's address.
    assert video_ref("https://example.com/clip.mp4") is None
    assert video_ref("https://www.youtube.com/watch?v=short") is None
    assert video_ref("javascript:alert(1)") is None


def test_times_and_addresses_in_chat() -> None:
    assert (seconds("90"), seconds("1:30"), seconds("1:02:03"), seconds("2m")) == (
        90,
        90,
        3723,
        120,
    )
    assert seconds("soon") is None
    found = videos_in("look (https://youtu.be/abcdefghijk), not https://example.com/x")
    assert [(address, ref.id) for address, ref in found] == [
        ("https://youtu.be/abcdefghijk", "abcdefghijk")
    ]


def test_the_steps_for_pictures_and_video() -> None:
    steps = parse_steps(
        """
        image card: cell.jpg
        generate made: a red circle
        video: https://youtu.be/abcdefghijk
        show: video | board
        play: at 1:20 for 30
        play:
        stop:
        """
    )
    assert [s.verb for s in steps] == ["image", "generate", "video", "show", "play", "play", "stop"]
    assert (steps[4].start, steps[4].seconds) == (80, 30)
    assert (steps[5].start, steps[5].seconds) == (-1, 0)
    for bad in (
        "image a: not-a-picture.txt",
        "video: https://example.com/x",
        "play: for ever",
        "play: for 99999",
        "show: video | video",
        "stop: now",
    ):
        with pytest.raises(StepError):
            parse_steps(bad)


def test_a_deck_can_hold_a_video() -> None:
    deck = parse_deck("---\nid: d\n---\nslide: One\nvideo: clip.mp4\n", "bio")
    item = deck["slides"][0]["items"][0]
    assert item["kind"] == "video"
    assert item["video"] == {"kind": "file", "id": "bio/clip.mp4", "form": ""}


# -- what the class is sent ---------------------------------------------------------------


def test_a_picture_is_a_line_on_the_board() -> None:
    board = Board()
    image, made = parse_steps("image card: cell.jpg\ngenerate made: a red circle")
    for step in (image, made):
        for ev in board_events(step, folder="bio"):
            board.apply(ev)
    card, pending = board.lines
    assert (card.kind, card.content, card.pending) == ("image", "lesson-files/bio/cell.jpg", False)
    assert pending.pending and pending.content == "" and "being made" in pending.describe()
    # The picture is ready after the board has been wiped: it is still filled in.
    board.apply({"type": "board.clear"})
    board.apply({"type": "board.fill", "id": "made", "content": "class-files/room/1.png"})
    filled = Board.from_snapshot(board.snapshot()).pages[0][1]
    assert (filled.content, filled.pending, filled.failed) == (
        "class-files/room/1.png",
        False,
        False,
    )
    assert filled.describe() == "a picture: a red circle"


def test_the_video_is_one_thing_the_whole_class_is_at_the_same_moment_of() -> None:
    stage = Stage()
    load, show, play_for, talk_over, stop = parse_steps(
        "video: clip.mp4\nshow: video | board\nplay: at 10 for 5\nplay:\nstop:"
    )
    with pytest.raises(ValueError):
        stage_events(play_for, stage)  # nothing to play yet
    for step in (load, show, play_for):
        for ev in stage_events(step, stage, folder="bio"):
            stage.apply(ev)
    assert stage.video["src"] == {"kind": "file", "id": "bio/clip.mp4", "form": ""}
    assert stage.video["playing"] and not stage.video["quiet"]
    # Someone who joins later is told where it is now, not where it started.
    stage._since -= 3
    late = Stage.from_snapshot(stage.snapshot())
    assert 12.9 <= late.video["position"] <= 13.5 and late.video["playing"]
    (again,) = stage_events(talk_over, stage)
    assert again["quiet"] is True and again["position"] >= 12.9  # from where it is
    (stopped,) = stage_events(stop, stage)
    stage.apply(stopped)
    assert not stage.video["playing"] and stage_events(stop, stage) == []


def test_play_means_the_video_on_the_slide_that_is_showing() -> None:
    stage = Stage()
    deck = parse_deck("---\nid: d\n---\nslide: One\nvideo: clip.mp4\n", "bio")
    stage.apply({"type": "slides.load", "deck": deck})
    stage.apply({"type": "stage.show", "rows": [[{"source": "slides"}]]})
    (play,) = parse_steps("play: for 3")
    load, start = stage_events(play, stage)
    assert load == {
        "type": "video.load",
        "src": deck["slides"][0]["items"][0]["video"],
        "position": 0,
    }
    assert start["type"] == "video.play" and start["position"] == 0


def test_time_to_watch_is_a_wait_and_the_video_is_stopped_after_it() -> None:
    steps = parse_steps("say: Watch this.\nplay: for 20\nsay: What did you see?")
    said, watch, stop, after = utterances_from_steps(steps, "seg")
    assert said.board == []  # the video starts after the sentence, not with it
    assert (watch.pause, watch.pause_text, watch.watching) == (20, WATCHING, True)
    assert [s.verb for s in watch.board] == ["play"] and [s.verb for s in stop.board] == ["stop"]
    assert after.text == "What did you see?"
    # Played with no time to watch, it rides on the sentence like any board step.
    (over,) = utterances_from_steps(parse_steps("say: Look.\nplay:"), "seg")
    assert [s.verb for s in over.board] == ["play"]


# -- pictures made during the class -------------------------------------------------------


class Pretend:
    """An image service of each kind, answering with the same small picture."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.status = 200
        self.picture = PNG

    async def images(self, request: web.Request) -> web.Response:
        self.sent.append(await request.json())
        if self.status != 200:
            return web.json_response({"error": "no"}, status=self.status)
        return web.json_response({"data": [{"b64_json": base64.b64encode(self.picture).decode()}]})

    async def chat(self, request: web.Request) -> web.Response:
        self.sent.append(await request.json())
        address = "data:image/png;base64," + base64.b64encode(self.picture).decode()
        message = {"role": "assistant", "content": "", "images": [{"image_url": {"url": address}}]}
        return web.json_response({"choices": [{"message": message}]})


@pytest.fixture
async def pretend() -> AsyncIterator[tuple[Pretend, str]]:
    server = Pretend()
    app = web.Application()
    app.router.add_post("/v1/images/generations", server.images)
    app.router.add_post("/v1/chat/completions", server.chat)
    running = TestServer(app)
    await running.start_server()
    try:
        yield server, str(running.make_url("/v1"))
    finally:
        await running.close()


async def test_the_image_model_is_asked_in_the_way_its_service_takes(
    pretend: tuple[Pretend, str],
) -> None:
    server, url = pretend
    extra = {"size": "1024x1024"}
    own = ImageModel("local:m", "m", base_url=url, api_key="", provider="local", extra=extra)
    assert await own.make("a red circle") == (PNG, "png")
    assert server.sent[0]["model"] == "m" and server.sent[0]["size"] == "1024x1024"
    assert server.sent[0]["prompt"].endswith("a red circle")
    through = ImageModel("openrouter:m", "m", base_url=url, api_key="k", provider="openrouter")
    assert await through.make("a red circle") == (PNG, "png")
    assert server.sent[1]["modalities"] == ["image", "text"]
    # What comes back must be a picture, and a failure is said, not passed on.
    server.picture = b"<html>not a picture</html>"
    with pytest.raises(ModelError):
        await own.make("a red circle")
    server.status = 500
    with pytest.raises(ModelError):
        await own.make("a red circle")
    assert picture_type(b"\xff\xd8\xff\xe0") == "jpg" and picture_type(b"GIF89a") is None


async def test_a_made_picture_fills_its_place_or_closes_it(
    pretend: tuple[Pretend, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, url = pretend
    monkeypatch.setenv("CH_GENERATED_DIR", str(tmp_path))
    events = ClassEvents(PretendRoom())  # type: ignore[arg-type]
    decider = VoiceDecider(PretendVoice(), events, gap_ms=0)  # type: ignore[arg-type]
    model = ImageModel("local:m", "m", base_url=url, api_key="", provider="local")
    decider.pictures = Pictures(events, model, "a room/../x", most=1)
    voice = asyncio.create_task(decider.run())
    decider.enqueue_steps(
        parse_steps(
            "say: Here is one.\ngenerate a: a red circle\nsay: And another.\ngenerate b: a square"
        ),
        source="script",
    )
    await until(
        lambda: len(events.board.lines) == 2 and not any(ln.pending for ln in events.board.lines)
    )
    voice.cancel()
    first, second = events.board.lines
    # It is saved in the class's own folder, whatever the room is called.
    assert first.content.startswith("class-files/a_room____x/1-") and first.content.endswith(".png")
    assert (tmp_path / first.content.removeprefix("class-files/")).read_bytes() == PNG
    # The class may make only so many: the second has no picture, and says so.
    assert second.failed and second.content == "" and "could not be made" in second.describe()
    assert len(server.sent) == 1


async def test_with_no_image_model_the_lesson_goes_on_without_the_picture() -> None:
    events = ClassEvents(PretendRoom())  # type: ignore[arg-type]
    decider = VoiceDecider(PretendVoice(), events, gap_ms=0)  # type: ignore[arg-type]
    voice = asyncio.create_task(decider.run())
    decider.enqueue_steps(parse_steps("say: Here.\ngenerate a: a circle\nnote b: text"), source="s")
    await until(lambda: len(events.board.lines) == 1)
    await decider.wait_until_idle()
    voice.cancel()
    assert [ln.id for ln in events.board.lines] == ["b"]


# -- the live teacher and videos ----------------------------------------------------------


async def test_a_shared_video_is_shown_to_the_teacher_who_may_then_play_it() -> None:
    link = "https://youtu.be/abcdefghijk"
    teacher, writer, decider = make(
        [
            ["at: 1", "say: Hello.", "pause: 3 Share what you found"],
            [
                # One from memory is not played. The one from the chat is.
                "video: https://youtu.be/zzzzzzzzzzz",
                f"video: {link}",
                "show: video",
                "say: Let us watch a little of that.",
                "play: for 2",
            ],
            ["say: Thank you for that.", "end:"],
        ]
    )

    async def look_up(ref: Any) -> str:
        return f'a {ref.site} video: "How cells divide", from A Channel'

    teacher._look_up = look_up
    loads: list[dict[str, Any]] = []
    emit = teacher._events.emit

    async def keep(*events: dict[str, Any]) -> None:
        loads.extend(ev for ev in events if ev["type"].startswith("video."))
        await emit(*events)

    teacher._events.emit = keep  # type: ignore[method-assign]

    async def during() -> None:
        await until(lambda: teacher._wait is not None and teacher._wait.began is not None)
        teacher.on_chat("a", f"this explains it {link}")

    await run(teacher, decider, during)
    shown = next(text for text in writer.shown if "How cells divide" in text)
    assert f"To show it: `video: {link}`" in shown
    assert [ev["type"] for ev in loads] == ["video.load", "video.play", "video.stop"]
    assert loads[0]["src"]["id"] == "abcdefghijk"
    # `play: for` ended the writer's turn, as a pause does.
    assert len(writer.shown) == 3
