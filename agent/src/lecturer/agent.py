"""The lecturer agent: one LiveKit agent process, one job per class room.

    uv run python -m lecturer.agent dev

Started from livekit-examples/agent-starter-python (MIT). The lecturer does not use
that starter's AgentSession: the voice, the speech queue and the floor are our own
plain code, because a lecture is not a one-to-one conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from livekit import api, rtc
from livekit.agents import AgentServer, AutoSubscribe, JobContext, cli

from .job import LecturerJob, is_on
from .models.chat_answers import ChatAnswerer
from .models.decisions import make_decisions
from .models.writer_planner import Planner, Writer
from .room.chat import Chat, ChatMessage
from .room.events import ClassEvents
from .room.floor import STAFF, Floor, person_of, role_of
from .room.groups import KEEPER, Groups, lecturer_api
from .room.pictures import Pictures
from .room.polls import Polls
from .room.react import Reactions
from .room.screen import ScreenShare
from .script.lesson import load_prep, load_segment
from .settings import image_model, limit, model_for
from .speech.decider import VoiceDecider
from .speech.hearing import Hearing
from .speech.holds import Hold
from .speech.tts import make_tts
from .speech.voice import SilentVoice, Voice
from .teaching.overseer import Overseer
from .teaching.questions import Questions
from .teaching.teacher import Teacher

logger = logging.getLogger("lecturer")

ROOT = Path(__file__).resolve().parents[3]
load_dotenv(ROOT / "agent" / ".env.local")

AGENT_NAME = "lecturer"
END_LINGER_SECONDS = 120
GOING_BACK = "We are going back to the main class now."
REPORT_SECONDS = 8


def _when(person: Any) -> float:
    """When someone joined, as a number, whatever LiveKit gives it as (or 0 if not yet)."""
    joined = getattr(person, "joined_at", None)
    if joined is None:
        return 0.0
    return float(joined.timestamp()) if hasattr(joined, "timestamp") else float(joined)


async def _cancel(task: asyncio.Task[Any]) -> None:
    task.cancel()


def group_brief(group: dict[str, Any]) -> str:
    """What the lecturer of a group is told, on top of the lesson prep."""
    said = " ".join(group.get("said") or []) or "(nothing yet)"
    what = group.get("brief") or "Teach this group the lesson as it is prepared."
    private = (
        " This is one student, taken aside for a short time with a human helper present. "
        "If they are not using it to learn, or have what they needed, finish kindly and "
        "end it: do not let it become a private chat."
        if group.get("private")
        else ""
    )
    return (
        "# This is a group\n\n"
        f'The class has split into groups, and you are teaching one of them: "{group["title"]}". '
        f"What this group is for: {what}{private}\n\n"
        f"The main class had just heard: {said}\n\n"
        f"You have about {group.get('minutes', 10):g} minutes. Do not begin the lesson again "
        "from the top, and do not split this group further. Do what the group is for. When "
        "the time is nearly up, or the group has what it came for, say so and write `end:`. "
        "Its students then go back to the main class."
    )


# Lecturers this process runs at once: one for each class, and one for each of its groups.
MAX_CLASSES = int(os.environ.get("CH_MAX_CLASSES", 8))


def class_load(server: AgentServer) -> float:
    """How full this process is, counted in classes.

    LiveKit sends a class to a lecturer process only if its load is low enough. The
    default measure is the whole machine's CPU use, so on a busy machine no lecturer
    would be sent at all. A class costs little CPU here: the heavy work is done by
    outside services.
    """
    return min(len(server.active_jobs) / MAX_CLASSES, 1.0)


# Full at MAX_CLASSES: the load is then 1.0, over the threshold, and LiveKit sends
# the next class elsewhere (or nowhere, if this is the only lecturer process).
server = AgentServer(load_fnc=class_load, load_threshold=0.99)


@server.rtc_session(agent_name=AGENT_NAME)
async def lecture(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}
    # Nothing here chooses a lesson. Whoever sends for the lecturer names it (`job.py`).
    job = LecturerJob.from_json(ctx.job.metadata)
    lesson, pause_scale, start_delay = job.lesson, job.pause_scale, job.start_delay
    # Set when this lecturer teaches a group that has split from a class (`groups.py`).
    group = job.group
    segment = load_segment(lesson)
    # Live: the class is written as it is taught, from the lesson prep. Otherwise the
    # script is read as written. Live needs a writer model and a script with parts.
    # Which model does which job is in `settings.py`.
    live = job.live
    writer_model = model_for("writer") if live else None
    if live and writer_model is None:
        logger.warning("asked to teach live, but there is no writer model: reading the script")
    elif live and not segment.parts:
        logger.warning("asked to teach live, but the script has no parts: reading it")
    live = writer_model is not None and bool(segment.parts)
    # One class can go without the planner even when a planner model is set.
    planner_model = model_for("planner") if live and job.planner else None

    # Students' tracks are not needed until someone is given the floor.
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_NONE)

    # Two students joining at the same moment can each send for a lecturer. One is
    # enough: the later one leaves.
    me = ctx.room.local_participant
    for other in ctx.room.remote_participants.values():
        is_lecturer = other.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        # The earlier one stays. The identity settles a tie.
        earlier = (_when(other), other.identity) < (_when(me), me.identity)
        if is_lecturer and earlier:
            logger.warning("this room already has a lecturer, leaving")
            ctx.shutdown("the room already has a lecturer")
            return

    stamp = time.strftime("%Y%m%d-%H%M%S")
    events = ClassEvents(ctx.room, log_path=ROOT / "logs" / f"{ctx.room.name}-{stamp}.jsonl")
    events.register()
    if group is not None:
        events.group = {"title": group["title"], "main": group["main"], "back": False}
    events.note(
        "class", room=ctx.room.name, lesson=lesson, live=live, planner=planner_model is not None
    )

    groups: Groups | None = None
    screen: ScreenShare | None = None

    async def on_shutdown() -> None:
        events.close()
        if screen is not None:
            await screen.close()
        if groups is not None:
            await groups.shut()

    ctx.add_shutdown_callback(on_shutdown)

    engine = make_tts()
    if engine is None:
        logger.info("no voice is set (CH_TTS): the class is in captions only")
    voice = Voice(ctx.room, engine) if engine is not None else SilentVoice()
    await voice.start()
    screen = ScreenShare(ctx.room)

    def screen_changed() -> None:
        # A tab opened, changed or got its title: tell the class, so that browsers
        # can draw the tab strip.
        task = asyncio.create_task(events.emit({"type": "screen.state", **screen.state()}))
        screen_tasks.add(task)
        task.add_done_callback(screen_tasks.discard)

    screen_tasks: set[asyncio.Task[None]] = set()
    screen.on_change = screen_changed
    decider = VoiceDecider(voice, events, screen=screen, pause_scale=pause_scale)
    # A picture or video named without a folder is in the lesson's own.
    decider.folder = lesson.split("/")[0]
    if (images := image_model()) is not None:
        decider.pictures = Pictures(events, images, ctx.room.name, most=limit("images"))
    run_task = asyncio.create_task(decider.run())
    floor = Floor(ctx.room, events, decider)
    decider.floor = floor
    floor.register()
    floor_task = asyncio.create_task(floor.run())
    reactions = Reactions(ctx.room, events, decider)
    decider.reactions = reactions
    reactions.register()
    polls = Polls(ctx.room, events)
    decider.polls = polls
    polls.register()

    if group is None:
        # A class can split into groups. A group cannot split again.
        # A group taught from a brief is taught live, which needs a writer model and a
        # lesson the live teacher can teach (one with parts).
        groups = Groups(
            ctx.room,
            events,
            lesson=lesson,
            can_teach_live=model_for("writer") is not None and bool(segment.parts),
        )
        groups.register()
        decider.groups = groups
        groups.on_done = decider.end_groups_wait
        # While students are on their way back, nothing is said to a half-empty room.
        groups.on_returning = lambda on: (
            decider.hold(Hold.GROUPS_RETURNING) if on else decider.resume(Hold.GROUPS_RETURNING)
        )
        # Another model keeps an eye on the groups, if there is one to do it.
        if (watcher := model_for("planner") or model_for("writer")) is not None:
            overseeing = asyncio.create_task(Overseer(groups, watcher, events).run())
            ctx.add_shutdown_callback(lambda: _cancel(overseeing))

    def person(identity: str) -> str:
        """Who an identity belongs to: someone here in two tabs is one person."""
        return person_of(ctx.room.remote_participants.get(identity), identity)

    def name_of(who: str) -> str:
        """The name a person gave, from any tab of theirs that is here."""
        for p in ctx.room.remote_participants.values():
            if person_of(p) == who and p.name:
                return p.name
        return ""

    def students() -> int:
        agent = rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        people = ctx.room.remote_participants.values()
        return len({person_of(p) for p in people if p.kind != agent and p.identity != KEEPER})

    def student_ids() -> list[str]:
        agent = rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
        people = ctx.room.remote_participants.values()
        # By person: someone here in two tabs is one student, whichever tabs are open.
        return sorted(
            {
                person_of(p)
                for p in people
                if p.kind != agent and role_of(p) == "student" and p.identity != KEEPER
            }
        )

    teacher: Teacher | None = None
    questions: Questions | None = None
    if live and writer_model is not None:
        teacher = Teacher(
            load_prep(lesson),
            decider,
            events,
            Writer(writer_model),
            students=student_ids,
            decisions=make_decisions(),
            planner=Planner(planner_model) if planner_model is not None else None,
            # A group has the time its split gave it, not the whole lesson's.
            minutes=job.minutes,
            brief=group_brief(group) if group is not None else "",
            writer_calls=limit("writer"),
            planner_calls=limit("planner"),
            # Students by name as well as label (the default), or by label only.
            name_of=None if os.environ.get("CH_STUDENT_NAMES", "").strip() == "labels" else name_of,
            # Testing only, with the students' agreement: everything the teacher saw
            # and did, students' words included, kept to compare runs of a class.
            record=ROOT / "logs" / "recorded" / f"{ctx.room.name}-{stamp}.jsonl"
            if is_on(os.environ.get("CH_RECORD_CLASS", ""))
            else None,
        )
        reactions.listener = lambda who, kind: teacher.on_reaction(person(who), kind)
        polls.on_vote = teacher.note_vote
    elif (quick := model_for("questions")) is not None:
        # Reading the script: chat questions are answered on the side.
        answerer = ChatAnswerer(
            quick,
            model_for("answers"),
            fast_calls=limit("questions"),
            strong_calls=limit("answers"),
        )
        questions = Questions(events, decider, answerer, f"{segment.course}, {segment.title}")
    else:
        logger.info("no model is set for chat questions: they will not be answered")

    def on_chat(message: ChatMessage) -> None:
        # The log records that a message came, not its words.
        events.note("chat", chars=len(message.text))
        if teacher is not None:
            teacher.on_chat(person(message.identity), message.text)
        elif questions is not None:
            questions.on_chat(message)

    Chat(ctx.room, on_chat).register()

    # What is said aloud. Like chat, the log records that someone spoke, not the words.
    def on_heard(identity: str, text: str) -> None:
        events.note("speech", chars=len(text))
        who = ctx.room.remote_participants.get(identity)
        if teacher is not None:
            teacher.on_speech(person(identity), text, staff=role_of(who) in STAFF)

    # Only a live teacher uses what is heard: a class read from its script sends nothing.
    hearing_model = model_for("hearing") if teacher is not None else None
    hearing = Hearing(ctx.room, hearing_model, on_heard, most=limit("hearing"))
    hearing.register()
    if teacher is not None:
        floor.on_turn = lambda who, spoke: teacher.on_floor_turn(
            person(who), spoke, can_hear=hearing.model is not None
        )

    def on_people(*_: rtc.RemoteParticipant) -> None:
        # Nobody to teach: stop talking. Carry on when someone comes back. (If nobody
        # does, LiveKit closes the room and this job with it.)
        # This hold is by the count of people alone. Other holds (a student with the
        # floor, a helper's pause) are separate and do not stand in for it.
        if groups is not None and groups.away:
            return  # everyone is in a group: the main lecturer waits for them
        if students() == 0 and not decider.held_for(Hold.EMPTY_ROOM):
            logger.info("the room is empty, holding")
            decider.hold(Hold.EMPTY_ROOM)
        elif students() > 0 and decider.held_for(Hold.EMPTY_ROOM):
            logger.info("someone is here, carrying on")
            decider.resume(Hold.EMPTY_ROOM)

    if groups is not None:
        groups.on_change = on_people

    # A group's lecturer tells the main class how it is going, and is told when to end,
    # through its own room's metadata.
    called_back = asyncio.Event()

    def room_meta() -> dict[str, Any]:
        with contextlib.suppress(ValueError):
            return dict(json.loads(ctx.room.metadata or "{}"))
        return {}

    def on_room_meta(*_: Any) -> None:
        if room_meta().get("close"):
            called_back.set()

    async def report(lk: Any, began: float) -> None:
        status: dict[str, Any] = {"phase": events.phase}
        if not (group or {}).get("private"):
            status.update(
                minutes_in=round((time.monotonic() - began) / 60, 1),
                students=students(),
                last_said=list(events.said)[-3:],
            )
        with contextlib.suppress(Exception):
            await lk.room.update_room_metadata(
                api.UpdateRoomMetadataRequest(
                    room=ctx.room.name, metadata=json.dumps({**room_meta(), "status": status})
                )
            )

    async def reporting(lk: Any) -> None:
        began = time.monotonic()
        while True:
            await report(lk, began)
            await asyncio.sleep(REPORT_SECONDS)

    closed = asyncio.Event()
    ctx.room.on("room_metadata_changed", on_room_meta)
    ctx.room.on("participant_connected", on_people)
    ctx.room.on("participant_disconnected", on_people)
    ctx.room.on("disconnected", lambda *_: closed.set())

    async def until_closed(coro: Coroutine[Any, Any, Any]) -> bool:
        """Wait for `coro`, giving up if the room closes. True if it finished."""
        task = asyncio.ensure_future(coro)
        closing = asyncio.ensure_future(closed.wait())
        await asyncio.wait({task, closing}, return_when=asyncio.FIRST_COMPLETED)
        task.cancel()
        closing.cancel()
        if task.done() and not task.cancelled() and task.exception() is not None:
            # Not a class that finished: one that broke. Say so, loudly.
            logger.error("the class stopped with an error", exc_info=task.exception())
            events.note("crashed", error=type(task.exception()).__name__)
        return not closed.is_set()

    async def until_called_back(coro: Coroutine[Any, Any, Any]) -> bool:
        """As `until_closed`, and a group also stops when the main class calls it back."""
        task = asyncio.ensure_future(coro)
        calling = asyncio.ensure_future(called_back.wait())
        finished = await until_closed(
            asyncio.wait({task, calling}, return_when=asyncio.FIRST_COMPLETED)
        )
        task.cancel()
        calling.cancel()
        if task.done() and not task.cancelled() and task.exception() is not None:
            logger.error("the class stopped with an error", exc_info=task.exception())
            events.note("crashed", error=type(task.exception()).__name__)
        return finished

    # The class starts a few seconds after the first person arrives (start_delay).
    # A group's lecturer says at once that it is here, so the main class can tell a
    # group still waiting for its students from a group that has no lecturer.
    lk = lecturer_api() if group is not None else None
    reporter: asyncio.Task[None] | None = None
    try:
        if lk is not None:
            await report(lk, time.monotonic())
        logger.info("waiting for the first student")
        on_room_meta()
        if not await until_called_back(ctx.wait_for_participant()):
            return
        if called_back.is_set():
            # A group nobody came to, ended by the main class before it began.
            logger.info("called back before anyone came")
            await events.emit({"type": "phase", "phase": "ended"})
            if lk is not None:
                await report(lk, 0)
            return
        await asyncio.sleep(start_delay)

        logger.info("teaching %s%s", lesson, " live" if live else "")
        await events.emit({"type": "phase", "phase": "teaching"})
        reporter = asyncio.create_task(reporting(lk)) if lk is not None else None
        on_room_meta()  # it may have been called back before it began
        if teacher is not None:
            finished = await until_called_back(teacher.run())
            use = teacher.use()
            events.note("model_use", **use)
            logger.info("model use: %s", use)
        else:
            decider.enqueue_segment(segment)
            finished = await until_called_back(decider.wait_until_idle())
        if finished and group is not None:
            # A group ends by sending its students back to the main class.
            if called_back.is_set():
                decider.clear()
                decider.end_pause()  # the goodbye is not kept waiting behind a pause
                await until_closed(decider.interject(GOING_BACK))
            if not await until_closed(floor.wait_free()):
                return
            await events.emit({"type": "split.back", "room": group["main"]})
            await events.emit({"type": "phase", "phase": "ended"})
            if reporter is not None:
                reporter.cancel()
            await report(lk, 0)
            await until_closed(asyncio.sleep(6))
        elif finished:
            # A student may still have the floor when the script runs out.
            if not await until_closed(floor.wait_free()):
                return
            # Questions that were put aside are answered now, before the class ends.
            if questions is not None:
                if not await until_closed(questions.keep_promises()):
                    return
                if not await until_closed(decider.wait_until_idle()):
                    return
            # Office hours have no script to get through: stay until their end time.
            stay = job.stay_seconds
            if stay > 0 and not await until_closed(asyncio.sleep(stay)):
                return
            await events.emit({"type": "phase", "phase": "ended"})
            logger.info("the lesson is over")
            # Stay so that late students see that it ended, then leave.
            await until_closed(asyncio.sleep(END_LINGER_SECONDS))
    finally:
        if reporter is not None:
            reporter.cancel()
        if lk is not None:
            with contextlib.suppress(Exception):
                await lk.aclose()
        run_task.cancel()
        floor_task.cancel()
        voice.stop()
        await screen.close()
        await floor.close()
        await hearing.close()
    if not closed.is_set():
        ctx.shutdown("the lesson is over")


if __name__ == "__main__":
    cli.run_app(server)
