"""The live teacher: the class is written as it is taught.

A model (the writer) writes the class a few sentences at a time, in the same step
language as a script, and the voice decider says them. This module is the loop
around the writer. It decides only *when* the writer is asked and *what it is
shown*. What the teacher then does is the writer's own judgement: there is no list
of situations here with a response for each.

The writer is asked for more:

    low         the voice has nearly caught up with what was written
    wait over   a wait the teacher set (a thinking pause, a poll) has ended, by the
                clock or because every student has responded
    respond     something has come in that is meant for the teacher. What was
                written and not yet said is taken back, so the teacher can change
                course after the sentence being spoken.

Waiting is a real state. A pause ends the writer's turn: nothing after it is
written until the wait is over and the writer can be shown what the class actually
did in it. If the writer is asked while the class is still working, whatever it
writes replaces the rest of the wait; to let them carry on, it writes a pause.

Each time, the writer is shown the class as it is: the clock, who is here, what has
been heard, the board and the stage, the chat, votes, reactions, how the class has
done on each task so far, and what it has put aside. The whole class is one
conversation with the writer that only ever grows, so it remembers what it said and
why.

A student may paste the address of a video in the chat (`links.py`). The writer is
told what it is and decides whether to show it. The writer can show a video from the
web only if its address is in the lesson prep or came from the chat: one it has
remembered or made up is not played.

The decision model (`decisions.py`) sorts each chat message: meant for the teacher,
urgent, or an answer to the task (and, if the teacher said what the answer is, right
or wrong). That is how code knows when to ask the writer, and how the writer knows
how quickly the class is getting it.

If the writer cannot be reached, the script is the safe default: the next part of
it is read as written, and the writer is asked again after.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import os
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import decisions as ask
from ..models.decisions import Decisions
from ..models.writer_planner import ModelError, Plan, Planner, Writer
from ..room.events import ClassEvents
from ..room.floor import spoken_name
from ..script.lesson import Part, Prep
from ..script.media import VideoRef, video_ref, videos_in
from ..script.steps import Step, StepParser
from ..speech.decider import Utterance, VoiceDecider, utterances_from_steps
from .links import describe_video
from .overseer import reports_text

logger = logging.getLogger("lecturer.teacher")

PROMPTS = Path(__file__).resolve().parent / "prompts"


def prompt(name: str) -> str:
    """A prompt, from CH_PROMPTS_DIR if that folder has one by this name, else ours
    (`prompts/`, which are one way to teach, not the only one)."""
    own = os.environ.get("CH_PROMPTS_DIR", "").strip()
    if own and (Path(own).expanduser() / name).is_file():
        return (Path(own).expanduser() / name).read_text()
    return (PROMPTS / name).read_text()


LOW_SECONDS = 9.0  # ask for more when less than this much speech is waiting
LATE_SECONDS = 90.0  # a message this soon after a task's wait may still answer it
WORDS_PER_SECOND = 2.6  # for estimating how long a sentence takes to say
BURST_SECONDS = 0.8  # let messages that come together be answered together
RESPOND_EVERY = 5.0  # at most one change of course in this many seconds
OVERRUN_SECONDS = 90.0  # how far past the end the class may run before it is stopped
ALL_IN_GRACE = 1.0  # after the last student responds, before a wait is ended early
CLOSING = "We are out of time, so I will stop there. Thank you all for coming."
STARTERS = ("say", "pause", "title", "clear")  # steps that begin a new utterance
MAX_SAYS = 8  # a turn is cut off after this many `say` lines, to stay quick to change


ADDRESS = re.compile(r"https?://[^\s<>\"'`]+")


def addresses_in(text: str) -> list[str]:
    return [a.rstrip(".,;:!?)") for a in ADDRESS.findall(text)]


def page_key(address: str) -> str:
    """An address as compared: without a trailing slash or a #part."""
    return address.strip().split("#")[0].rstrip("/")


HANDLE = re.compile(r"\bs\d{1,3}\b")  # how students are shown to the writer: s1, s2 ...


INVITED = "(a student is invited to speak)"


def naming(names: bool) -> str:
    """What the writer is told about naming students, by the CH_STUDENT_NAMES setting."""
    if names:
        return (
            "# Naming students\n\nStudents are shown to you by the name they gave, with a "
            "label: Ada (s1). You may use their names as a good teacher does, warmly, to "
            "thank or include someone; never to put anyone on the spot, or to show who got "
            "something wrong. Never say a label aloud. In a `split: one` step, use the "
            "label.\n\n"
        )
    return (
        "# Naming students\n\nStudents are shown to you as s1, s2 and so on. Those are "
        'labels for you, never said aloud: nobody is named in class (say "one of you"). '
        "In a `split: one` step, use the label.\n\n"
    )


def marked(right: bool | None) -> str:
    """How a typed answer is marked for the writer, once the decision model has said."""
    return {True: " (right)", False: " (not the right answer, yet)", None: ""}[right]


def clock(seconds: float) -> str:
    seconds = max(0, round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def speech_seconds(text: str) -> float:
    return len(text.split()) / WORDS_PER_SECOND


@dataclass
class ChatLine:
    who: str  # identity
    handle: str  # what the writer is shown instead of a name: s1, s2 ...
    text: str
    at: float
    kind: str = ""  # "" until sorted, then: urgent | for you | answer | chatter
    right: bool | None = None  # for an answer, if the teacher gave the right one
    shown: bool = False  # the writer has been shown it
    scene: str = ""  # how it was put to the decision model
    video: str = ""  # a video whose address is in the message, in words for the writer
    spoken: bool = False  # said aloud (and written down), not typed


@dataclass
class Wait:
    """A pause the teacher set, and what the class did in it."""

    text: str
    seconds: float
    expect: str = ""  # the right answer, if the teacher gave one
    began: float | None = None
    ended: float | None = None  # class time
    waited: float = 0.0
    early: bool = False
    over: bool = False
    replaced: bool = False  # the teacher moved on before it ended
    students: int = 0
    done: set[str] = field(default_factory=set)
    answers: dict[str, ChatLine] = field(default_factory=dict)
    # Who has responded during this wait itself. A wait that gives more time on a task
    # keeps the earlier `done` and `answers`, but only these end it early.
    since: set[str] = field(default_factory=set)
    poll: dict[str, Any] | None = None
    groups: bool = False  # the wait is the class's time in groups


@dataclass
class Written:
    """Hung on each utterance the teacher writes."""

    snapshot: StepParser  # the script reader as it was before these lines were read
    wait: Wait | None = None


class Teacher:
    def __init__(
        self,
        prep: Prep,
        decider: VoiceDecider,
        events: ClassEvents,
        writer: Writer,
        *,
        students: Callable[[], list[str]],
        decisions: Decisions | None = None,
        planner: Planner | None = None,
        minutes: float | None = None,
        start_at: str = "",
        time_scale: float = 1.0,
        look_up: Callable[[VideoRef], Awaitable[str]] = describe_video,
        brief: str = "",
        record: Path | None = None,
        writer_calls: int | None = None,
        planner_calls: int | None = None,
        name_of: Callable[[str], str] | None = None,
    ) -> None:
        """`start_at` begins the class at that part, as if the parts before it had been
        taught (for trying one stretch of a lesson). `time_scale` runs the class clock
        faster than real time, for simulations; the voice must be sped up to match.
        `record`: a file to write everything in `trace` to as it happens, students'
        words included. For testing, with the students' agreement only.
        `writer_calls`, `planner_calls`: the most calls this class may make to each
        (CH_WRITER_LIMIT, CH_PLANNER_LIMIT). Past the writer's, the rest of the class
        is read from the script; past the planner's, the plan stays as it is.
        `name_of`: a student's name, from who they are. Given, the models are shown
        students by name as well as by label, and the teacher may use names in class
        (CH_STUDENT_NAMES=names). Not given, only by label (labels)."""
        self._prep = prep
        self._decider = decider
        self._events = events
        self._writer = writer
        self._decisions = decisions
        self._planner = planner
        self._students = students
        self._length = (minutes or prep.segment.minutes or 20) * 60
        self._scale = time_scale
        self._deck_name = next((st.text for st in prep.segment.steps if st.verb == "slides"), "")
        self._folder = decider.folder or prep.segment.course
        self._look_up = look_up
        # The videos from the web that may be shown: the ones in the lesson prep, and
        # (added as they come) the ones students share.
        prepared = f"{prep.script}\n{prep.notes}\n{prep.deck}"
        self._videos = {ref.key for _, ref in videos_in(prepared, most=200)}
        # Web pages that may be put on the shared screen: the same rule as for videos.
        self._pages = {page_key(a) for a in addresses_in(prepared)}
        self._start_at = start_at

        lesson = (
            f"# The lesson prep\n\nLesson: {prep.segment.title}. "
            f"Time slot: {self._length / 60:g} minutes.\n\n"
            f"## Teaching notes\n\n{prep.notes or '(none)'}\n\n"
            f"## The script\n\n{prep.script}\n\n"
            f"## The slide deck\n\n{prep.deck or '(none)'}\n"
        )
        # A lecturer teaching a group that split from a class is told what it is for.
        self._group = bool(brief)
        if brief:
            lesson += f"\n{brief}\n"
        self._told: list[str] = []  # things that went wrong, for the writer to be shown
        self._system = prompt("writer.md") + "\n\n" + naming(name_of is not None) + lesson
        self._plan_system = prompt("planner.md") + "\n\n" + lesson

        self._messages: list[dict[str, Any]] = []  # the conversation with the writer
        self._parser = StepParser(strict=False)
        self._buffer: list[Step] = []  # steps read and not yet given to the voice
        self._buffer_snapshot = copy.deepcopy(self._parser)
        self._expect = ""  # the answer to the task about to be set
        self._carry: Wait | None = None  # a wait the next pause would be continuing

        self._began = 0.0
        self._wake = asyncio.Event()
        self._asked = ""  # "", "respond" or "urgent": something has come in
        self._writing = ""  # why the writer is being asked now
        self._responded_at = -1e9
        self._written = 0  # utterances given to the voice
        self._quiet_until = 0.0
        self._ended = False
        self._failures = 0
        self._empty = 0

        self._speaking: str | None = None
        self._heard: list[tuple[str, str]] = []  # (source, text) since the writer was last told
        self._said: list[str] = []  # every lesson sentence heard, for the decision model
        self._transcript: list[str] = []  # the same, with waits, for the planner
        self._chat: list[ChatLine] = []  # not yet shown to the writer
        self._reactions: list[tuple[str, str]] = []  # (kind, who) not yet shown
        self._handles: dict[str, str] = {}  # identity -> label: s1, s2 ...
        self._name_of = name_of
        self._asks: dict[str, int] = {}  # handle -> messages meant for the teacher
        self._wait: Wait | None = None  # the wait in progress
        self._wait_over: Wait | None = None  # one that has ended and not been shown
        self._record: list[Wait] = []  # every wait that has ended
        self._asides: list[str] = []
        self._early: dict[str, dict[str, ChatLine]] = {}  # poll id -> answers before its pause
        self._says = 0  # `say` lines in the turn being read
        self._full = False  # the turn has had enough of them
        self._at = ""  # the part the teacher says it is in
        self._plan: Plan | None = None
        self._plan_is_new = False
        self._planning: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

        # What happened, for people testing this. It holds students' words, so it is
        # kept in memory only and never written to the class log.
        self.trace: deque[dict[str, Any]] = deque(maxlen=2000)
        self._record_file = record
        self._writer_calls = writer_calls
        self._planner_calls = planner_calls
        if record is not None:
            record.parent.mkdir(parents=True, exist_ok=True)

    def use(self) -> dict[str, Any]:
        """What the models cost in this class, and how quickly the writer answered."""
        firsts = sorted(self._writer.use.first_line_ms)
        out: dict[str, Any] = {"writer": self._writer.use.as_dict()}
        if firsts:
            out["writer_first_line_ms"] = {
                "median": round(firsts[len(firsts) // 2]),
                "worst": round(firsts[-1]),
            }
        if self._planner is not None:
            out["planner"] = self._planner.use.as_dict()
        calls = getattr(self._decisions, "calls", None)
        if calls is not None:
            out["decisions"] = {
                "calls": calls,
                "tokens_in": getattr(self._decisions, "tokens_in", 0),
                "tokens_out": getattr(self._decisions, "tokens_out", 0),
            }
        return out

    # -- time and people ---------------------------------------------------------------

    def _now(self) -> float:
        """Class time, in seconds."""
        return asyncio.get_running_loop().time() * self._scale

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self._scale)

    def _elapsed(self) -> float:
        return self._now() - self._began

    def _left(self) -> float:
        return self._length - self._elapsed()

    def _trace(self, kind: str, **fields: Any) -> None:
        entry = {"t": round(self._elapsed(), 1), "kind": kind, **fields}
        self.trace.append(entry)
        if self._record_file is not None:
            try:
                with self._record_file.open("a") as f:
                    f.write(json.dumps(entry, default=str) + "\n")
            except OSError:
                logger.exception("could not write the class record")

    def _shown(self, utt: Utterance) -> str:
        """A sentence as the models are shown it. The line that invites a student to
        speak says their name: with names off, the models are not shown it."""
        if utt.source == "invite" and self._name_of is None:
            return INVITED
        return utt.text

    def _handle(self, identity: str) -> str:
        """A student as the models are shown them: a label, s1, and with names on,
        their name too: "Ada (s1)"."""
        if identity not in self._handles:
            self._handles[identity] = f"s{len(self._handles) + 1}"
        label = self._handles[identity]
        name = spoken_name(self._name_of(identity)) if self._name_of else ""
        return f"{name} ({label})" if name else label

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- what the class does -----------------------------------------------------------

    def on_chat(self, identity: str, text: str) -> None:
        """A student typed in the chat."""
        # One line: what a student types is shown to the models inside quotes.
        text = " ".join(text.split())
        line = ChatLine(identity, self._handle(identity), text, self._now())
        self._chat.append(line)
        self._trace("chat", who=line.handle, text=text)
        self._spawn(self._sort(line))

    def on_speech(self, identity: str, text: str, *, staff: bool = False) -> None:
        """Someone said something aloud, and it was written down (`speech/hearing.py`)."""
        text = " ".join(text.split())
        handle = "a helper" if staff else self._handle(identity)
        line = ChatLine(identity, handle, text, self._now(), spoken=True)
        self._chat.append(line)
        self._trace("speech", who=handle, text=text)
        self._spawn(self._sort(line))

    def on_floor_turn(self, identity: str, spoke: bool, *, can_hear: bool) -> None:
        """A student's turn with the floor (a raised hand) has ended."""
        handle = self._handle(identity)
        if not spoke:
            self._told.append(
                f"{handle} raised a hand and was given the floor, but nothing was heard from "
                "them: their microphone may not be working. Ask whether they still have a "
                "question, and say they can type it in the chat."
            )
        elif not can_hear:
            self._told.append(
                f"{handle} raised a hand and spoke, but you cannot hear what people say in "
                "this class. Say so kindly, and ask them to type it in the chat."
            )
        else:
            return  # what they said comes as speech
        self._trace("turn", who=handle, spoke=spoke)
        if not self._asked:
            self._asked = "respond"
        self._wake.set()

    def on_reaction(self, identity: str, kind: str) -> None:
        """A student pressed thumbs up, thumbs down or done."""
        self._trace("reaction", who=self._handle(identity), reaction=kind)
        if kind == "done":
            if self._wait is not None:
                self._wait.done.add(identity)
                self._wait.since.add(identity)
                self._check_all_in(self._wait)
        else:
            self._reactions.append((kind, identity))

    async def _sort(self, line: ChatLine) -> None:
        """Ask the decision model what a chat message is, and act on the answer."""
        wait = self._wait
        # Just after a wait, a message may still answer its task: people finish late.
        late = None
        if wait is None and self._record:
            last = self._record[-1]
            if last.ended is not None and self._now() - last.ended < LATE_SECONDS:
                late = last
        # A poll can be open before the pause for it starts: answers count from then.
        poll = wait.poll if wait is not None else None
        if poll is None and self._poll_is_open():
            poll = self._latest_poll()
        doing = ""
        if wait is not None or late is not None:
            doing = f'The lecturer had set the class this task: "{(wait or late).text}"'
        if poll:
            options = " / ".join(poll["options"])
            doing += (" with" if doing else "The lecturer had opened") + (
                f' a poll: "{poll["question"]}" ({options})'
            )
        task = wait is not None or late is not None or poll is not None
        self._pages |= {page_key(a) for a in addresses_in(line.text)}
        shared = videos_in(line.text)
        if shared:
            address, ref = shared[0]
            self._videos.add(ref.key)
            try:
                about = await self._look_up(ref)
            except Exception:
                logger.exception("could not describe a shared video")
                about = f"a {ref.site} video"
            line.video = f"{about}. To show it: `video: {address}`"
        scene = ask.scene(self._prep.segment.title, self._said, line.text, doing)
        line.scene = scene
        questions = [ask.for_teacher(scene), ask.urgent(scene)]
        if task:
            questions.append(ask.is_answer(scene))
        answers: list[float | None] = [None] * len(questions)
        if self._decisions is not None:
            try:
                answers = await self._decisions.ask_many(questions)
            except Exception:
                logger.exception("the decision model failed")
        for_me, is_urgent = answers[0], answers[1]
        an_answer = answers[2] if task else None

        if is_urgent is not None and is_urgent >= 0.7:
            line.kind = "urgent"
        elif an_answer is not None and an_answer >= 0.6 and an_answer >= (for_me or 0):
            line.kind = "answer"
        elif for_me is None or for_me >= 0.5:
            # Not sure, or the model was late: the teacher sees it and decides.
            line.kind = "for you"
        else:
            line.kind = "chatter"
        if line.video and line.kind == "chatter":
            line.kind = "for you"  # whether to show it is the teacher's call
        self._trace("sorted", who=line.handle, text=line.text, as_=line.kind, p=answers)

        if line.kind == "answer" and wait is not None:
            # The newest message from each student is the one that counts.
            wait.answers[line.who] = line
            wait.since.add(line.who)
            await self._mark(line, wait.expect)
            self._check_all_in(wait)
        elif line.kind == "answer" and late is not None:
            # Shown to the teacher with the chat, as an answer to the last task.
            await self._mark(line, late.expect)
            self._transcript.append(
                f'(After the wait "{late.text}", {line.handle} answered "{line.text}"'
                f"{marked(line.right)}.)"
            )
        elif line.kind == "answer" and poll is not None:
            # Kept until the pause for this poll begins.
            self._early[poll["id"]] = {**self._early.get(poll["id"], {}), line.who: line}
        elif line.kind in ("urgent", "for you"):
            self._asks[line.handle] = self._asks.get(line.handle, 0) + 1
            if line.kind == "urgent":
                self._asked = "urgent"
            elif not line.shown and not self._asked:
                self._asked = "respond"  # (if it has been shown, the teacher has seen it)
            self._wake.set()

    async def _mark(self, line: ChatLine, expect: str) -> None:
        """Ask whether a typed answer is right, if the teacher said what is right."""
        if not expect or self._decisions is None or line.right is not None:
            return
        with contextlib.suppress(Exception):
            (p,) = await self._decisions.ask_many([ask.is_right(line.scene, expect)])
            line.right = None if p is None else p >= 0.5

    def _voters(self, wait: Wait) -> int:
        """How many of the students here have voted in the wait's poll (not helpers,
        nor people who have left). Brings the wait's counts up to date."""
        latest = self._latest_poll()
        if wait.poll and latest and latest["id"] == wait.poll["id"]:
            wait.poll = latest
        if not wait.poll:
            return 0
        polls = self._decider.polls
        if polls is None:
            return sum(wait.poll["counts"])
        return len(polls.voters() & set(self._students()))

    def _latest_poll(self) -> dict[str, Any] | None:
        polls = self._events.polls.polls
        return dict(polls[-1], counts=list(polls[-1]["counts"])) if polls else None

    def _check_all_in(self, wait: Wait) -> None:
        """End a wait early once every student has responded in some way."""
        here = set(self._students())
        students = len(here)
        # Working typed in the chat is not the same as being finished. When the task
        # has a right answer, a typed message counts once it has reached it; pressing
        # done always counts. (Without one, as for "what did you find?", any reply does.)
        finished = set(wait.since) & here  # only students count, not helpers
        if wait.expect:
            finished = {
                who
                for who in finished
                if who in wait.done or (who in wait.answers and wait.answers[who].right)
            }
        responded = max(len(finished), self._voters(wait))
        if wait is self._wait and students and responded >= students:

            async def end_soon() -> None:
                await self._sleep(ALL_IN_GRACE)
                if wait is self._wait:
                    self._decider.end_pause()

            self._spawn(end_soon())

    def note_vote(self) -> None:
        """A vote came in on the open poll."""
        if self._wait is not None:
            self._check_all_in(self._wait)

    # -- what the voice does -----------------------------------------------------------

    def _on_voice(self, what: str, utt: Utterance, info: dict[str, Any]) -> None:
        wait = utt.tag.wait if isinstance(utt.tag, Written) else None
        if what == "split.failed":
            self._told.append(f"The split was not done: {info['why']}. The class is together.")
        elif what == "start":
            self._speaking = self._shown(utt)
        elif what == "heard":
            self._speaking = None
            self._heard.append((utt.source, self._shown(utt)))
            if utt.source not in ("ready", "invite"):
                self._said.append(utt.text)
                self._transcript.append(utt.text)
            if utt.text:
                self._trace("heard", text=utt.text)
        elif what == "pause" and wait is not None:
            wait.began = self._now()
            wait.students = len(self._students())
            wait.poll = self._latest_poll() if self._poll_is_open() else None
            if wait.poll:
                for who, line in self._early.pop(wait.poll["id"], {}).items():
                    wait.answers.setdefault(who, line)
                    wait.since.add(who)
                    self._spawn(self._mark(line, wait.expect))
            self._wait = wait
            self._trace("pause", text=wait.text, seconds=wait.seconds)
            self._check_all_in(wait)
        elif what == "pause.end" and wait is not None:
            wait.over, wait.early = True, info["early"]
            wait.ended = self._now()
            wait.waited = info["waited"] * self._scale
            self._voters(wait)  # the final votes
            if wait is self._wait:
                self._wait = None
            self._record.append(wait)
            self._transcript.append(f"({self._wait_summary(wait)})")
            self._trace("pause.end", text=wait.text, waited=round(wait.waited), early=wait.early)
            if not wait.replaced:
                self._wait_over = wait
        self._wake.set()

    def _poll_is_open(self) -> bool:
        polls = self._events.polls.polls
        return bool(polls and polls[-1]["open"])

    def _waiting_speech(self) -> float:
        """Seconds of speech written and not yet begun."""
        return sum(speech_seconds(u.text) for u in self._decider.unspoken("writer"))

    def _pause_ahead(self) -> bool:
        return self._wait is not None or any(u.pause for u in self._decider.unspoken("writer"))

    def _idle(self) -> bool:
        return (
            self._speaking is None and self._wait is None and not self._decider.unspoken("writer")
        )

    # -- the loop ----------------------------------------------------------------------

    async def run(self) -> None:
        """Teach the class, to its end."""
        self._began = self._now()
        if self._start_at:
            ids = [p.id for p in self._prep.segment.parts]
            if self._start_at not in ids:
                raise ValueError(f"no part called {self._start_at!r}")
            index = ids.index(self._start_at)
            self._at = ids[index - 1] if index else ""
        self._decider.listeners.append(self._on_voice)
        self._decider.name_segment(self._prep.segment.id, self._prep.segment.title)
        reason = "start"
        try:
            while reason:
                write = asyncio.create_task(self._write(reason))
                write.add_done_callback(lambda _: self._wake.set())
                try:
                    reason = await self._next_reason(write)
                finally:
                    if not write.done():
                        write.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await write
            await self._decider.wait_until_idle()
        finally:
            self._decider.listeners.remove(self._on_voice)
            for task in [*self._tasks, self._planning]:
                if task is not None:
                    task.cancel()

    async def _next_reason(self, write: asyncio.Task[None]) -> str:
        """Wait until the writer should be asked again. "" when the class is over."""
        while True:
            await self._wake.wait()
            self._wake.clear()
            now = self._now()

            if not self._ended and self._left() < -OVERRUN_SECONDS:
                # Well past the end, and still going. Stop it here.
                self._trace("stopped", why="over time")
                self._ended = True
                if not write.done():
                    write.cancel()
                self._decider.drop_unspoken("writer")
                self._decider.end_pause()
                await self._decider.interject(CLOSING)
                return ""

            if self._asked and not self._ended:
                if not write.done():
                    if self._writing != "low" and self._asked != "urgent":
                        continue  # it is already answering something: let it finish
                    write.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await write
                if self._asked != "urgent":
                    since = now - self._responded_at
                    await self._sleep(max(BURST_SECONDS, RESPOND_EVERY - since))
                reason, self._asked = self._asked, ""
                return reason

            if not write.done():
                continue
            if self._ended:
                if self._idle():
                    return ""
                continue
            if self._wait_over is not None:
                return "wait over"
            if self._pause_ahead():
                continue
            if now < self._quiet_until:
                delay = (self._quiet_until - now) / self._scale
                asyncio.get_running_loop().call_later(delay, self._wake.set)
                continue
            if self._waiting_speech() < LOW_SECONDS:
                return "low"

    async def _write(self, reason: str) -> None:
        """Ask the writer for what comes next, and give it to the voice as it arrives."""
        if self._writer_calls is not None and self._writer.use.calls >= self._writer_calls:
            # Out of writer calls for this class: the script, a part at a time.
            if self._writing != "limit":
                self._events.note("writer_limit", calls=self._writer.use.calls)
                self._trace("limit", job="writer")
            self._writing = "limit"
            if not self._ended and self._waiting_speech() < LOW_SECONDS:
                self._read_script()
            return
        self._writing = reason
        dropped: list[Utterance] = []
        if reason in ("respond", "urgent"):
            self._responded_at = self._now()
            dropped = self._take_back()
        # What the writer is about to be told. Kept, so that it can be put back if
        # the writer never sees it.
        wait_over, self._wait_over = self._wait_over, None
        chat, self._chat = self._chat, []
        reactions, self._reactions = self._reactions, []
        heard, self._heard = self._heard, []
        self._carry = wait_over or self._wait

        update = self._update(reason, dropped, wait_over, chat, reactions, heard)
        for line in chat:
            line.shown = True
        self._messages.append({"role": "user", "content": update})
        self._trace("asked", reason=reason, update=update)
        began = self._now()
        got: list[str] = []
        self._says, self._full = 0, False
        first_ms: float | None = None
        written_before = self._written
        failed = False
        try:
            async with contextlib.aclosing(self._writer.lines(self._system, self._messages)) as ls:
                async for line in ls:
                    if first_ms is None:
                        first_ms = (self._now() - began) / self._scale * 1000  # real time
                    if self._read(line):
                        if not self._full:
                            got.append(line)  # (a line past the limit was not used)
                        break  # a pause, the end, or enough: nothing more is used
                    got.append(line)
            self._failures = 0
        except ModelError as e:
            failed = True
            logger.warning("%s", e)
            self._events.note("writer_failed", reason=reason, error=str(e))
            self._trace("failed", error=str(e))
        finally:
            # Whatever was read is given to the voice, even if the writer was cut off:
            # anything not wanted is taken back by the next turn, and so reported.
            self._flush()
            if got:
                self._messages.append({"role": "assistant", "content": "\n".join(got)})
            elif failed:
                # It never saw this update. Put back what it would have been told.
                self._messages.pop()
                self._chat = chat + self._chat
                self._reactions = reactions + self._reactions
                self._heard = heard + self._heard
                self._wait_over = self._wait_over or wait_over
            else:
                self._messages.append({"role": "assistant", "content": "(cut off)"})
            wrote = self._written - written_before
            self._events.note(
                "writer",
                reason=reason,
                first_line_ms=None if first_ms is None else round(first_ms),
                lines=len(got),
                utterances=wrote,
            )
            self._trace("wrote", reason=reason, first_line_ms=first_ms, lines=got)

        if failed:
            self._failures += 1
            self._quiet_until = self._now() + 2
            if self._failures >= 2 or self._waiting_speech() < 3:
                self._failures = 0
                self._read_script()
        elif wrote == 0 and not self._ended:
            # It chose to say nothing. Do not ask again until something changes.
            self._empty += 1
            self._quiet_until = self._now() + 10
            if self._empty >= 3:
                self._empty = 0
                self._read_script()
        else:
            self._empty = 0

    def _take_back(self) -> list[Utterance]:
        """Take back what was written and not yet begun, and wind the reader back."""
        dropped = self._decider.drop_unspoken("writer")
        if dropped and isinstance(dropped[0].tag, Written):
            self._parser = copy.deepcopy(dropped[0].tag.snapshot)
        return dropped

    # -- reading what the writer writes ------------------------------------------------

    def _read(self, line: str) -> bool:
        """Take one line from the writer. True if its turn must end here."""
        verb, colon, rest = line.partition(":")
        if not colon:
            self._trace("bad line", line=line)
            return False
        word = verb.split()[0].lower() if verb.split() else ""
        rest = rest.strip()

        # Lines for us, not for the class.
        if word in ("at", "part"):
            # (`part:` is how the script writes it; the id is what matters.)
            self._set_at(rest.split("|")[0].strip())
        elif word == "why":
            self._trace("why", text=rest)
        elif word == "work":
            # Its own working out, never shown or said. What it wrote before is given
            # to the voice now, so the class is not kept waiting while it works.
            self._flush()
            self._trace("work", text=rest)
        elif word == "aside":
            if rest:
                self._asides.append(rest)
        elif word == "kept":
            if rest.isdigit() and 1 <= int(rest) <= len(self._asides):
                self._asides.pop(int(rest) - 1)
        elif word == "expect":
            self._expect, self._carry = rest, None
        elif word == "replan":
            self._ask_planner(rest or "the lecturer asked")
        elif word == "end":
            self._flush()
            self._ended = True
            return True
        else:
            return self._read_step(word, line)
        return False

    @staticmethod
    def _shared(line: ChatLine) -> str:
        return f" [they shared {line.video}]" if line.video else ""

    def _read_step(self, word: str, line: str) -> bool:
        groups = self._decider.groups
        if (
            word == "regroup"
            and groups is not None
            and self._wait is not None
            and self._wait.groups
        ):
            # Called back early, now: this cannot wait in the queue behind the wait.
            groups.coming_back()
            self._spawn(groups.close(line.partition(":")[2].strip()))
            return False
        if word == "video":
            ref = video_ref(line.partition(":")[2], self._folder)
            if ref is not None and ref.kind != "file" and ref.key not in self._videos:
                # An address from memory may be wrong, or not what it is thought to be.
                self._trace("bad line", line=line, why="a video not from the prep or the chat")
                return False
        if word in ("open", "prepare"):
            address = line.partition(":")[2].strip()
            if page_key(address) not in self._pages:
                # Only pages from the prep or the chat: one from memory may be wrong,
                # and the writer must not be talked into showing anything at all.
                self._trace("bad line", line=line, why="a page not from the prep or the chat")
                self._told.append(
                    f"The page {address} was not opened: only pages from the lesson prep or "
                    "the chat can be shown."
                )
                return False
        if word in STARTERS:
            self._flush()
            if self._full:
                return True
        if word == "say" and HANDLE.search(line):
            # The labels students are shown by are never said aloud.
            self._trace("named", line=line)
            line = HANDLE.sub("one of you", line)
        if not self._buffer:
            self._buffer_snapshot = copy.deepcopy(self._parser)
        step = self._parser.feed(line)
        if step is None:
            if word not in ("gap", "column"):
                self._trace("bad line", line=line)
            return False
        if step.verb == "poll" and step.style != "close":
            self._carry = None  # a new question, not more time on the old one
        self._buffer.append(step)
        if step.verb == "split" and step.style == "one":
            # The writer names a student by handle. The rest of the code knows identities.
            # By the label, however the writer wrote the rest ("s3", "Ada (s3)").
            label = HANDLE.search(step.text)
            who = next((i for i, h in self._handles.items() if label and h == label.group(0)), "")
            if not who:
                self._buffer.pop()
                self._trace("bad line", line=line, why="no such student")
                return False
            step.text = who
        waits = step.verb == "pause" or (step.verb == "play" and step.seconds > 0)
        if waits or (step.verb == "split" and step.style != "one"):
            # A wait: the turn ends, and the writer is asked again when it is over.
            self._flush()
            return True
        if step.verb == "say":
            self._says += 1
            if self._says >= MAX_SAYS:
                # Long enough. Its board steps may follow, so the turn ends at the
                # next line that starts something new.
                self._full = True
        return False

    def _flush(self) -> None:
        """Give the steps read so far to the voice."""
        if not self._buffer:
            return
        steps, self._buffer = self._buffer, []
        utterances = utterances_from_steps(steps, self._prep.segment.id, "writer")
        for utt in utterances:
            wait = None
            if utt.pause:
                wait = Wait(utt.pause_text, utt.pause, self._expect, groups=utt.split is not None)
                if self._carry is not None and not utt.watching:
                    # More time on the same task: keep what the class has done so far.
                    # (Time to watch a video is never that.) Copies, so that what is
                    # typed in this wait is not added to the last one's record.
                    wait.done, wait.answers = set(self._carry.done), dict(self._carry.answers)
                    wait.expect = wait.expect or self._carry.expect
                self._expect = ""
            utt.tag = Written(self._buffer_snapshot, wait)
            self._written += 1
        if self._wait is not None and not self._wait.groups:
            # The class was waiting, and the teacher has decided what comes next.
            # (Not so for time in groups: what is written then waits for them to be
            # back. Only `regroup:` calls them back early.)
            self._wait.replaced = True
            self._decider.end_pause()
        self._decider.enqueue(*utterances)

    def _set_at(self, part_id: str) -> None:
        known = {p.id for p in self._prep.segment.parts}
        if part_id in known and part_id != self._at:
            self._at = part_id
            self._trace("at", part=part_id)
            self._ask_planner(f"the lecturer has reached part {part_id}")

    def _parts_left(self) -> list[Part]:
        parts = self._prep.segment.parts
        index = next((i for i, p in enumerate(parts) if p.id == self._at), -1)
        return parts[index + 1 :]

    def _read_script(self) -> None:
        """The safe default: put the next part of the script in the queue as written."""
        left = self._parts_left()
        if not left:
            self._ended = True
            self._decider.enqueue(Utterance(text=CLOSING, source="writer"))
            return
        part = left[0]
        logger.warning("reading part %s from the script", part.id)
        self._events.note("script_fallback", part=part.id)
        self._trace("script", part=part.id)
        told = (
            f"(The writer could not be reached, so part {part.id} is being read from the script.)"
        )
        if self._messages and self._messages[-1]["role"] == "user":
            self._messages.append({"role": "assistant", "content": "(nothing)"})
        self._messages.append({"role": "user", "content": told})
        self._messages.append({"role": "assistant", "content": "\n".join(part.lines)})
        self._at = part.id
        for line in part.lines:
            self._read(line)
        self._flush()

    # -- what the writer is shown ------------------------------------------------------

    def _wait_summary(self, wait: Wait) -> str:
        n = wait.students or len(self._students())
        if wait.over:
            how = f"ran {wait.waited:.0f} s of {wait.seconds:.0f} s"
            if wait.early and not wait.replaced:
                how += ", ended early because everyone had responded"
            head = f'The wait "{wait.text}" is over: it {how}.'
        else:
            gone = self._now() - (wait.began or self._now())
            head = (
                f'The class is working on: "{wait.text}". {gone:.0f} s gone, '
                f"{max(0, wait.seconds - gone):.0f} s left."
            )
        bits = [head, f"{len(wait.done)} of {n} pressed done."]
        if wait.answers:
            shown = []
            for line in wait.answers.values():
                mark = marked(line.right)
                took = f", after {line.at - wait.began:.0f} s" if wait.began else ""
                aloud = " (said aloud)" if line.spoken else ""
                shown.append(f'{line.handle}{aloud} "{line.text}"{mark}{took}{self._shared(line)}')
            bits.append("Answers typed in the chat: " + "; ".join(shown) + ".")
        else:
            bits.append("No answers typed in the chat.")
        if wait.poll:
            counts = " · ".join(
                f"{option}: {count}"
                for option, count in zip(wait.poll["options"], wait.poll["counts"], strict=False)
            )
            voted = sum(wait.poll["counts"])
            bits.append(f'Poll "{wait.poll["question"]}": {counts}. {voted} of {n} voted.')
        return " ".join(bits)

    def _stage_text(self) -> str:
        stage, board = self._events.stage, self._events.board
        rows = " / ".join(" | ".join(p["source"] for p in row) for row in stage.rows)
        out = [f"On the stage: {rows}."]
        if stage.deck:
            slides = stage.deck["slides"]
            title = slides[stage.slide - 1]["title"] if 0 < stage.slide <= len(slides) else ""
            showing = "showing" if "slides" in rows else "loaded but not on the stage"
            out.append(f'Slides {showing}: slide {stage.slide} of {len(slides)}, "{title}".')
        elif self._deck_name:
            out.append(
                f"No slides loaded yet: students see no slide until you write "
                f"`slides: {self._deck_name}` and a `show:` with slides in it."
            )
        lines = []
        for ln in board.lines:
            marks = "".join(
                f" [{style}{' ' + part if part else ''}]" for part, style in ln.marks.items()
            )
            lines.append(f"  {ln.id} ({ln.kind}): {ln.describe()}{marks}")
        board_text = "\n".join(lines) if lines else "  (empty)"
        return " ".join(out) + "\nOn the board page showing:\n" + board_text

    def _update(
        self,
        reason: str,
        dropped: list[Utterance],
        wait_over: Wait | None,
        chat: list[ChatLine],
        reactions: list[tuple[str, str]],
        heard: list[tuple[str, str]],
    ) -> str:
        students = len(self._students())
        left = self._left()
        out = [
            f"[{clock(self._elapsed())} into the class, "
            + (f"{clock(left)} left" if left >= 0 else f"{clock(-left)} OVER TIME: finish now")
            + f". {students} student{'s' if students != 1 else ''} here.]"
        ]
        out.append(
            {
                "start": (
                    f"Part {self._start_at} is about to begin. The parts before it have been "
                    "taught, as in the script, and the students have just heard them. Carry "
                    "on from there."
                    if self._start_at
                    else "The group has just come together, and is listening. Begin."
                    if self._group
                    else "The class is starting now. The students are here and listening. Begin."
                ),
                "low": "Carry on: write what comes next.",
                "wait over": "The wait you set is over. Look at what the class did, then write "
                "what comes next.",
                "respond": "Something has come in for you. Decide what a good teacher would do "
                "now, and write what comes next.",
                "urgent": "Something has come in that needs you at once. Write what comes next.",
            }[reason]
        )

        # What went wrong, and how the groups went, if the class has just been in them.
        out.extend(self._told)
        self._told = []
        groups = self._decider.groups
        if groups is not None and groups.last and wait_over is not None:
            out.append(
                "The class was in groups, and is together again:\n" + reports_text(groups.last)
            )
            groups.last = []

        # What has been said.
        mine = [text for source, text in heard if source == "writer" and text]
        others = [text for source, text in heard if source != "writer" and text]
        if mine:
            out.append(f'Heard since you last wrote: {len(mine)} sentences, ending "{mine[-1]}"')
        if others:
            out.append("Also said, not written by you: " + " ".join(f'"{t}"' for t in others))
        if self._speaking:
            out.append(f'Being said right now: "{self._speaking}"')
        waiting = [u.text for u in self._decider.unspoken("writer") if u.text]
        if waiting:
            out.append(
                "Written and still to be said (they will be said: do not write them again, "
                "carry on after them): " + " ".join(f'"{t}"' for t in waiting)
            )
        taken = [u.text for u in dropped if u.text]
        if taken:
            out.append(
                "These were written and have NOT been said. They were taken back so that you "
                "can change course; say them again only if they still fit: "
                + " ".join(f'"{t}"' for t in taken)
            )
        if any(u.pause for u in dropped):
            out.append("A pause you had written was taken back too.")

        out.append(self._stage_text())
        groups = self._decider.groups
        if groups is not None and not self._group and not groups.open_now:
            most = groups.most_groups()
            out.append(
                f"Splitting the class into groups is possible now (up to {most} groups)."
                if most >= 2
                else "Splitting the class into groups is not possible now (not enough "
                "helpers or students here)."
            )

        # What the class has done.
        happened = []
        answers = {id(a) for w in [*self._record, self._wait] if w for a in w.answers.values()}
        for line in chat:
            if id(line) in answers:
                continue  # shown with its wait
            ago = self._now() - line.at
            tag = f" [{line.kind}]" if line.kind else ""
            tag += marked(line.right)
            count = self._asks.get(line.handle, 0)
            often = f" ({line.handle}'s message number {count} to you)" if count > 2 else ""
            how = " said aloud" if line.spoken else ""
            happened.append(
                f'- {line.handle}, {ago:.0f} s ago{how}: "{line.text}"{tag}{often}'
                f"{self._shared(line)}"
            )
        ups = len({who for kind, who in reactions if kind == "up"})
        downs = len({who for kind, who in reactions if kind == "down"})
        if ups or downs:
            happened.append(f"- Reactions: thumbs up from {ups}, thumbs down from {downs}.")
        if wait_over is not None:
            happened.append("- " + self._wait_summary(wait_over))
        if self._wait is not None:
            happened.append(
                "- "
                + self._wait_summary(self._wait)
                + " Whatever you write now replaces the rest of this wait. To let them carry "
                "on working, write only a pause."
            )
        elif self._poll_is_open() and (wait_over is None or not wait_over.poll):
            poll = self._latest_poll() or {}
            counts = " · ".join(
                f"{o}: {c}" for o, c in zip(poll["options"], poll["counts"], strict=False)
            )
            happened.append(f'- The poll "{poll["question"]}" is open: {counts}.')
        out.append(
            "Since you last wrote:\n" + "\n".join(happened)
            if happened
            else "Since you last wrote: nothing from the class."
        )

        earlier = [w for w in self._record if w is not wait_over][-3:]
        if earlier:
            out.append(
                "Earlier tasks:\n" + "\n".join(f"- {self._wait_summary(w)}" for w in earlier)
            )
        if self._asides:
            out.append(
                "Put aside, not yet answered:\n"
                + "\n".join(f"{n}. {text}" for n, text in enumerate(self._asides, 1))
            )

        parts = self._parts_left()
        if self._prep.segment.parts:
            script = sum(p.minutes for p in parts)
            ids = ", ".join(p.id for p in parts) or "none"
            out.append(
                f"You are in part {self._at or '(not said yet)'}. Parts after it: {ids}. "
                f"Read as scripted they take about {script:g} min, and there are "
                f"{max(0, left) / 60:.1f} min left."
            )
        if self._plan is not None:
            new = "The plan has changed. " if self._plan_is_new else ""
            self._plan_is_new = False
            out.append(f"{new}The plan for the rest of the class:\n{self._plan.as_text()}")
        return "\n\n".join(out)

    # -- the planner -------------------------------------------------------------------

    def _ask_planner(self, why: str) -> None:
        if self._planner is None or (self._planning and not self._planning.done()):
            return
        if self._planner_calls is not None and self._planner.use.calls >= self._planner_calls:
            return
        self._planning = asyncio.create_task(self._make_plan(why))

    async def _make_plan(self, why: str) -> None:
        assert self._planner is not None
        students = len(self._students())
        situation = "\n\n".join(
            [
                f"You are being asked because {why}.",
                f"{clock(self._elapsed())} into the class, {clock(self._left())} left. "
                f"{students} students here. The lecturer is in part {self._at or '?'}.",
                "What the class has heard so far, with what they did in each wait:\n"
                + "\n".join(self._transcript),
                "Put aside, not yet answered:\n" + ("\n".join(self._asides) or "(nothing)"),
                "The current plan:\n" + (self._plan.as_text() if self._plan else "(the script)"),
            ]
        )
        try:
            plan = await self._planner.plan(self._plan_system, situation)
        except ModelError as e:
            logger.warning("%s", e)
            self._trace("plan failed", error=str(e))
            return
        self._trace("plan", why=plan.why, changed=plan.changed, plan=plan.as_text())
        if plan.changed or self._plan is None:
            self._plan, self._plan_is_new = plan, True
