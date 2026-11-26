"""The voice decider: plain code that owns the one speech queue.

Everything the lecturer says goes through here, one sentence at a time. Each
sentence's board changes are sent at the moment its audio starts.

The live teacher (`teacher.py`) writes into this queue a few sentences ahead of
the voice. It listens for what has been said, and can take back what it wrote
that has not been said yet.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from livekit.agents import tokenize

from ..room.board import board_events
from ..room.events import ClassEvents
from ..room.floor import Floor
from ..room.groups import Groups
from ..room.pictures import Pictures
from ..room.polls import Polls
from ..room.react import Reactions
from ..room.screen import ScreenShare
from ..room.stage import Stage, stage_events
from ..script.lesson import Segment
from ..script.steps import SCREEN_VERBS, STAGE_VERBS, Step
from .voice import Clip, Voice

logger = logging.getLogger("lecturer.decider")

_sentences = tokenize.basic.SentenceTokenizer(min_sentence_len=12)

WATCHING = "Watching the video"
IN_GROUPS = "Working in groups"


@dataclass
class Utterance:
    """One thing in the speech queue: a sentence with its board changes, or a pause."""

    text: str = ""
    board: list[Step] = field(default_factory=list)
    pause: float = 0.0
    pause_text: str = ""
    source: str = "script"  # script | writer | ready | answer | invite
    pos: tuple[str, int] | None = None  # plan position: segment id, step number
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    clip: Clip | None = None
    retried: bool = False  # text-to-speech has already been tried twice
    done: asyncio.Future[None] | None = None  # set when it has been said (or given up on)
    hold_after: str = ""  # a hold to start the moment this has been said
    asked: float | None = None  # when the question this answers was typed
    tag: Any = None  # whoever wrote it may hang something here; the decider ignores it
    watching: bool = False  # the pause is time to watch a video, so it is never shortened
    split: Step | None = None  # the pause is time in groups, which this step sets up
    closes: str = ""  # it ends the wait with this id: the `stop` or `regroup` after it
    begun: bool = False  # it was started once and cut off, and is waiting to be said again


def utterances_from_steps(
    steps: list[Step], segment_id: str, source: str = "script"
) -> list[Utterance]:
    out: list[Utterance] = []
    last_say: Utterance | None = None
    for n, step in enumerate(steps):
        pos = (segment_id, n)
        if step.verb == "say":
            for sentence in _sentences.tokenize(step.text):
                last_say = Utterance(text=sentence, source=source, pos=pos)
                out.append(last_say)
        elif step.verb == "pause":
            out.append(Utterance(pause=step.seconds, pause_text=step.text, source=source, pos=pos))
            last_say = None
        elif step.verb == "play" and step.seconds > 0:
            # Time to watch: the video starts after the sentence before it, nothing is
            # said while it plays, and it is stopped when the time is up.
            out.append(
                Utterance(
                    board=[step],
                    pause=step.seconds,
                    pause_text=WATCHING,
                    source=source,
                    pos=pos,
                    watching=True,
                )
            )
            out.append(Utterance(board=[Step("stop")], source=source, pos=pos, closes=out[-1].id))
            last_say = None
        elif step.verb == "split" and step.style != "one":
            # The whole class goes into groups after the sentence before this, the main
            # lecturer waits while they work, and then everyone is brought back.
            out.append(
                Utterance(
                    pause=step.seconds,
                    pause_text=IN_GROUPS,
                    source=source,
                    pos=pos,
                    watching=True,
                    split=step,
                )
            )
            out.append(
                Utterance(board=[Step("regroup")], source=source, pos=pos, closes=out[-1].id)
            )
            last_say = None
        elif last_say is not None and step.verb not in ("title", "clear"):
            last_say.board.append(step)
        else:
            # A new page never rides on the sentence before it.
            last_say = None
            if not (out and not out[-1].text and not out[-1].pause):
                out.append(Utterance(source=source, pos=pos))
            out[-1].board.append(step)
    return out


class VoiceDecider:
    def __init__(
        self,
        voice: Voice,
        events: ClassEvents,
        *,
        screen: ScreenShare | None = None,
        prefetch: int = 2,
        gap_ms: int = 120,
        pause_scale: float = 1.0,
        watch_scale: float = 1.0,
    ) -> None:
        self._voice = voice
        self._events = events
        self._screen = screen
        self._screen_steps: asyncio.Queue[Step] = asyncio.Queue()
        self.floor: Floor | None = None  # set once the floor exists; it needs us too
        self.reactions: Reactions | None = None
        self.polls: Polls | None = None
        self.pictures: Pictures | None = None  # makes pictures, if there is an image model
        self.groups: Groups | None = None  # splits the class into groups
        self.folder = ""  # the lesson's own folder: where a file named without one is
        self._side_tasks: set[asyncio.Task[None]] = set()
        self._screen_worker: asyncio.Task[None] | None = None
        self._queue: deque[Utterance] = deque()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._prefetch_n = prefetch
        self._gap = gap_ms / 1000
        self._pause_scale = pause_scale
        # A video takes as long as it takes, so time to watch one is not shortened with
        # the thinking pauses. Only a simulated class, with no real video, changes this.
        self._watch_scale = watch_scale
        self._segment: str | None = None
        self._titles: dict[str, str] = {}
        self._skip_pause = asyncio.Event()
        self._skip_groups = asyncio.Event()  # ends time in groups, and nothing else does
        self._may_speak = asyncio.Event()
        self._may_speak.set()
        self._holds: set[str] = set()
        # Set when a hold cut the sentence being spoken. Read after the cut, because
        # the hold itself may already be over by then (a student who leaves and is
        # back within a moment), and the sentence must still be said again.
        self._cut_by_hold = False
        self._hold_changed = asyncio.Event()  # wakes a thinking pause when a hold starts
        # Told what happens to each utterance: "start" (the sentence begins), "heard"
        # (it was said to the end), "pause" and "pause.end" (a thinking pause; the
        # second comes with `early` and `waited`).
        self.listeners: list[Callable[[str, Utterance, dict[str, Any]], None]] = []

    # -- the queue ----------------------------------------------------------------

    def enqueue(self, *utterances: Utterance) -> None:
        self._queue.extend(utterances)
        self._idle.clear()
        self._wake.set()
        self._prefetch()

    def enqueue_segment(self, segment: Segment) -> None:
        self.name_segment(segment.id, segment.title)
        self.enqueue(*utterances_from_steps(segment.steps, segment.id))

    def name_segment(self, segment_id: str, title: str) -> None:
        """Give the title shown to the class while utterances of this segment play."""
        self._titles[segment_id] = title

    def unspoken(self, source: str) -> list[Utterance]:
        """What is waiting from this source and has not been started.

        The step that ends a wait already under way (the `stop` after time to watch a
        video, the `regroup` after time in groups) is not counted: the wait began, so
        it must still be ended, whatever is written in place of what followed it."""
        queued = {u.id for u in self._queue}
        return [
            u
            for u in self._queue
            if u.source == source and not u.begun and not (u.closes and u.closes not in queued)
        ]

    def drop_unspoken(self, source: str) -> list[Utterance]:
        """Take back everything from this source that has not been started, in order.

        A sentence that was cut off and is waiting to be said again stays: the class
        has already seen what it wrote on the board."""
        dropped = self.unspoken(source)
        if dropped:
            gone = {id(u) for u in dropped}
            self._queue = deque(u for u in self._queue if id(u) not in gone)
            for utt in dropped:
                if utt.clip is not None:
                    utt.clip.cancel()
            self._prefetch()
        return dropped

    def clear(self) -> None:
        """Take back everything that is waiting, whoever wrote it."""
        for utt in self._queue:
            if utt.clip is not None:
                utt.clip.cancel()
            if utt.done is not None and not utt.done.done():
                utt.done.set_result(None)
        self._queue.clear()

    def _tell(self, what: str, utt: Utterance, **info: Any) -> None:
        for listener in self.listeners:
            try:
                listener(what, utt, info)
            except Exception:
                logger.exception("a listener failed on %s", what)

    def _prefetch(self) -> None:
        """Start making audio for the next few sentences."""
        ahead = 0
        for utt in self._queue:
            if not utt.text:
                continue
            if utt.clip is None:
                utt.clip = self._voice.prepare(utt.text)
            ahead += 1
            if ahead >= self._prefetch_n:
                break

    async def wait_until_idle(self) -> None:
        await self._idle.wait()

    def interject(
        self, text: str, *, then_hold: str = "", source: str = "ready"
    ) -> asyncio.Future[None]:
        """Say this next, after the sentence being spoken. The result is ready once
        it has been said. Used for ready lines such as "Sam, go ahead".

        `then_hold` starts a hold the moment it has been said, so that the next
        sentence of the lesson does not begin before the student does."""
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        ready = Utterance(text=text, source=source, done=done, hold_after=then_hold)
        self._queue.appendleft(ready)
        self._idle.clear()
        self._wake.set()
        return done

    def _from_steps(self, steps: list[Step], source: str, asked: float | None) -> list[Utterance]:
        utterances = utterances_from_steps(steps, "", source)
        for utt in utterances:
            utt.pos = None  # not part of the lesson's plan
        if utterances and asked is not None:
            first = next((u for u in utterances if u.text), utterances[0])
            first.asked = asked
        return utterances

    def interject_steps(
        self, steps: list[Step], *, source: str, asked: float | None = None
    ) -> None:
        """Perform these steps next, after the sentence being spoken, then carry on."""
        for utt in reversed(self._from_steps(steps, source, asked)):
            self._queue.appendleft(utt)
        self._idle.clear()
        self._wake.set()
        self._prefetch()

    def enqueue_steps(self, steps: list[Step], *, source: str) -> None:
        """Perform these steps after everything already waiting."""
        self.enqueue(*self._from_steps(steps, source, None))

    def hold(self, reason: str = "hold") -> None:
        """Stop speaking now and say nothing more until every hold is lifted.

        Holds are kept by reason (an empty room, a student with the floor, a
        moderator's pause), so lifting one does not undo another. The sentence that
        was cut off is said again from its start afterwards.
        """
        self._holds.add(reason)
        self._cut_by_hold = True
        self._may_speak.clear()
        self._voice.stop()
        self._hold_changed.set()

    def resume(self, reason: str = "hold") -> None:
        self._holds.discard(reason)
        if not self._holds:
            self._may_speak.set()
            self._hold_changed.set()

    @property
    def held(self) -> bool:
        return not self._may_speak.is_set()

    def held_for(self, reason: str) -> bool:
        return reason in self._holds

    def stage_changed(self, rows: list[list[dict[str, Any]]]) -> None:
        """The stage was changed by something other than a script step (a person's
        shared screen). Tell the lecturer's own screen whether it is still on it."""
        self._screen_do(Step("show", rows=rows))

    # -- running ------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            if not self._queue:
                self._idle.set()
                self._wake.clear()
                await self._wake.wait()
                continue
            await self._may_speak.wait()
            if not self._queue:
                continue  # emptied while we waited (taken back, or cleared): look again
            utt = self._queue.popleft()
            self._prefetch()
            try:
                put_back = await self._perform(utt)
            except Exception:
                logger.exception("could not perform a %s sentence", utt.source)
                put_back = False
            # Unless it was put back to be said again, whoever is waiting on it may go on.
            if put_back:
                continue
            if utt.hold_after:
                self._holds.add(utt.hold_after)
                self._may_speak.clear()
            if utt.done and not utt.done.done():
                utt.done.set_result(None)

    def _step_events(
        self, steps: list[Step], sentence_ms: float | None = None
    ) -> list[dict[str, Any]]:
        """The events for a sentence's board and stage steps. Screen steps are started
        here too: they take a moment, so they run beside the sentence, not before it."""
        out: list[dict[str, Any]] = []
        scratch: Stage | None = None
        for step in steps:
            if step.verb in SCREEN_VERBS:
                self._screen_do(step)
            elif step.verb == "poll":
                if self.polls is not None:
                    if step.style == "close":
                        self._after(self.polls.close())
                    else:
                        self._after(self.polls.open(step.text, step.options))
            elif step.verb == "mic":
                if self.floor is not None:
                    self._after(self.floor.set_mode(step.style, step.text))
            elif step.verb in ("split", "regroup"):
                if self.groups is None:
                    self._events.note("step_failed", verb=step.verb, error="no groups here")
                elif step.verb == "regroup":
                    self.groups.coming_back()
                    self._after(self.groups.close(step.text))
                else:  # one student taken aside, while the class goes on
                    self._after(self._aside(step))
            elif step.verb == "generate" and self.pictures is None:
                # No image model: the lesson goes on without the picture.
                logger.warning("skipped a picture: no image model is set")
                self._events.note("step_failed", verb="generate", error="no image model")
            elif step.verb in STAGE_VERBS or step.on == "slide":
                if step.verb == "show":
                    self._screen_do(step)  # the screen takes the shape of its pane
                try:
                    # Worked out against a copy that each event is applied to, so that
                    # `slides:` then `slide: 2` on one sentence lands on the new deck.
                    if scratch is None:
                        scratch = Stage.from_snapshot(self._events.stage.snapshot())
                    made = stage_events(step, scratch, sentence_ms, self.folder)
                    for ev in made:
                        scratch.apply(ev)
                    out.extend(made)
                except Exception as e:  # for example a deck that does not exist
                    logger.warning("skipped %s: %s", step.verb, e)
                    self._events.note("step_failed", verb=step.verb, error=str(e))
                    continue
            else:
                try:
                    out.extend(board_events(step, sentence_ms, self.folder))
                except ValueError as e:
                    logger.warning("skipped %s: %s", step.verb, e)
                    self._events.note("step_failed", verb=step.verb, error=str(e))
                    continue
            if step.verb == "generate" and self.pictures is not None:
                self.pictures.make(step.id, step.text, step.on)
        return out

    async def _aside(self, step: Step) -> None:
        assert self.groups is not None
        why = await self.groups.aside(step, list(self._events.said))
        if why:
            self._events.note("split_failed", why=why)
            self._tell("split.failed", Utterance(), why=why)

    def _after(self, coro: Any) -> None:
        """Run something beside the sentence it belongs to."""
        task = asyncio.create_task(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    def _screen_do(self, step: Step) -> None:
        """Queue a step for the shared screen. They run one at a time, in order, so
        that tabs are opened in the order the script names them."""
        self._screen_steps.put_nowait(step)
        if self._screen_worker is None or self._screen_worker.done():
            self._screen_worker = asyncio.create_task(self._screen_work())

    async def _screen_work(self) -> None:
        while not self._screen_steps.empty():
            step = self._screen_steps.get_nowait()
            try:
                if self._screen is None:
                    raise RuntimeError("this class has no shared screen")
                if step.verb == "show":
                    rows = step.rows
                    with_screen = [r for r in rows if any(p["source"] == "screen" for p in r)]
                    if with_screen:
                        await self._screen.set_shape(len(with_screen[0]), len(rows))
                    else:
                        await self._screen.hide()
                elif step.verb == "open":
                    await self._screen.open(step.text)
                elif step.verb == "prepare":
                    await self._screen.prepare(step.text)
                elif step.verb == "tab":
                    await self._screen.switch(step.amount)
                else:
                    await self._screen.scroll(step.amount)
            except Exception as e:
                logger.warning("shared screen: %s", e)
                self._events.note("screen_failed", verb=step.verb, error=str(e))

    async def _perform(self, utt: Utterance) -> bool:
        """Say and show one utterance. True if it was put back to be said again."""
        first: list[dict[str, Any]] = []
        if utt.pos and utt.pos[0] != self._segment:
            first.append(
                {"type": "plan", "segment": utt.pos[0], "title": self._titles.get(utt.pos[0], "")}
            )

        if utt.text:
            clip = utt.clip or self._voice.prepare(utt.text)
            ready_in_time = False

            async def show(dur: float | None) -> None:
                """Send the sentence and its board and stage changes."""
                say: dict[str, Any] = {
                    "type": "say",
                    "id": utt.id,
                    "text": utt.text,
                    "source": utt.source,
                }
                if utt.pos:
                    say["pos"] = list(utt.pos)
                    self._segment = utt.pos[0]
                if dur:
                    say["dur_ms"] = round(dur)
                await self._events.emit(*first, say, *self._step_events(utt.board, dur))

            async def on_start() -> None:
                nonlocal ready_in_time
                ready_in_time = clip.ready
                if utt.asked is not None:
                    waited = round((time.monotonic() - utt.asked) * 1000)
                    self._events.note("answer_began", ms_after_question=waited)
                await show(clip.duration_ms if clip.ready else None)
                self._tell("start", utt)

            self._cut_by_hold = False
            played = await self._voice.play(clip, on_start)
            self._events.note(
                "tts",
                id=utt.id,
                chars=len(utt.text),
                first_audio_ms=round(clip.first_audio_ms)
                if clip.first_audio_ms is not None
                else None,
                audio_ms=round(clip.duration_ms),
                ready_before_needed=ready_in_time,
                failed=played.failed,
            )
            if played.completed:
                await self._events.emit({"type": "heard", "id": utt.id})
                self._tell("heard", utt)
                await asyncio.sleep(self._gap)
            elif not played.started and not played.failed:
                # Stopped before a sound was made and before anything was shown.
                # Put it back whole, to be said when the hold is lifted.
                utt.clip = clip
                self._queue.appendleft(utt)
                return True
            elif played.failed and not played.started and not utt.retried:
                # Text-to-speech gave nothing. Try once more before going without.
                logger.warning("text-to-speech failed for a %s sentence, trying again", utt.source)
                utt.retried = True
                utt.clip = None
                self._queue.appendleft(utt)
                return True
            elif played.failed and not played.started:
                # No voice for this sentence. Show it anyway and leave time to read
                # it, so the class is not rushed through a board nobody explained.
                logger.error("no voice for a %s sentence; showing it as a caption only", utt.source)
                self._events.note("voice_failed", id=utt.id)
                await show(None)
                self._tell("start", utt)
                await asyncio.sleep(1 + len(utt.text.split()) / 2.5)
                await self._events.emit({"type": "cut", "id": utt.id, "heard_ms": 0})
                self._tell("heard", utt)  # read as a caption, which is the best there is
            else:
                await self._events.emit(
                    {"type": "cut", "id": utt.id, "heard_ms": round(played.played_ms)}
                )
                if self._cut_by_hold:
                    # Say it again after the hold. Its board and stage steps were
                    # already sent when it started, so they are not repeated.
                    again = Utterance(
                        text=utt.text,
                        pause=utt.pause,
                        pause_text=utt.pause_text,
                        source=utt.source,
                        pos=utt.pos,
                        done=utt.done,
                        hold_after=utt.hold_after,
                        tag=utt.tag,
                        begun=True,
                    )
                    self._queue.appendleft(again)
                    return True
        elif utt.board or first:
            if utt.pos:
                self._segment = utt.pos[0]
            await self._events.emit(*first, *self._step_events(utt.board))

        if utt.split is not None:
            # Into groups. If that cannot be done, the class carries on together.
            why = "no groups here"
            if self.groups is not None:
                why = await self.groups.open(utt.split, list(self._events.said))
            if why:
                logger.warning("the class was not split: %s", why)
                self._events.note("split_failed", why=why)
                self._tell("split.failed", utt, why=why)
                return False

        if utt.pause:
            self._tell("pause", utt)
            began = asyncio.get_running_loop().time()
            scale = self._watch_scale if utt.watching else self._pause_scale
            early = await self._pause(
                utt.pause * scale, utt.pause_text, utt.watching, groups=utt.split is not None
            )
            waited = asyncio.get_running_loop().time() - began
            self._tell("pause.end", utt, early=early, waited=waited)
        return False

    async def _pause(
        self, seconds: float, text: str, watching: bool = False, groups: bool = False
    ) -> bool:
        """A thinking pause, time to watch a video, or time in groups. A hold stops its
        clock; it runs on when the hold lifts. True if it was cut short.

        Time in groups has an end signal of its own. A student pressing done, or the
        teacher writing something, ends a thinking pause; neither may call a whole
        class back from its groups."""
        left = seconds
        if self.reactions is not None and not groups:
            self.reactions.begin_task()
        ended = self._skip_groups if groups else self._skip_pause
        ended.clear()
        loop = asyncio.get_running_loop()
        while left > 0.05 and not ended.is_set():
            await self._may_speak.wait()
            if self.groups is not None and not self.groups.open_now and text == IN_GROUPS:
                break  # the groups have all ended
            pause: dict[str, Any] = {"type": "pause", "seconds": round(left, 1), "text": text}
            if watching:
                pause["watching"] = True
            # Cleared before the event is sent, so that a hold that begins while it is
            # being sent is not missed.
            self._hold_changed.clear()
            await self._events.emit(pause)
            began = loop.time()
            skip = asyncio.ensure_future(ended.wait())
            hold = asyncio.ensure_future(self._hold_changed.wait())
            done, _ = await asyncio.wait(
                {skip, hold}, timeout=left, return_when=asyncio.FIRST_COMPLETED
            )
            skip.cancel()
            hold.cancel()
            left -= loop.time() - began
            if hold not in done:
                break  # the time is up, or the pause was ended
            # A hold began (or began and ended): wait until the voice may go on, then
            # the rest of the pause runs.
        await self._events.emit({"type": "pause.end"})
        return ended.is_set()

    def end_pause(self) -> None:
        """Cut a thinking pause short."""
        self._skip_pause.set()

    def end_groups_wait(self) -> None:
        """Every group has ended: the main class need not wait out the time."""
        self._skip_groups.set()
