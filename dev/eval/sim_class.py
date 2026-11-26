"""A simulated class for the live teacher, with pretend students and a pretend voice.

The real teacher, speech queue, class events and polls run as in a class. Only
LiveKit, the browser and the voice are pretend: a sentence "takes" as long as it
would to say, divided by --speed. The models are real (the ones set in
`agent/.env.local`: see `settings.py`), so this spends a little.

    cd agent && uv run python ../dev/eval/sim_class.py <scenario.json>
    cd agent && uv run python ../dev/eval/sim_class.py <scenario.json> --no-planner

A scenario is written for one lesson, so it is kept with that lesson, not here. The
lesson needs `part:` lines. A scenario is a JSON file:

    {
      "lesson": "<course>/<lesson>",
      "start_at": "7",                   optional: begin at this part
      "minutes": 8,                      optional: the time slot, from the start
      "students": 6,
      "note": "what is being tried",
      "stop_after": "8",                 optional: stop when the teacher reaches this part
      "events": [
        {"when": {"pause": 1, "after": 5}, "chat": ["s2", "35"]},
        {"when": {"poll": 1, "after": 3}, "vote": ["s1", 1]},
        {"when": {"said": "partner", "after": 0}, "react": ["s3", "down"]},
        {"when": {"at": 40}, "chat": ["s4", "is this on the test?"]}
      ]
    }

`when` is one of: `at` (seconds of class time), `part` (the teacher says it has
reached this part), `pause` (the nth thinking pause starts), `poll` (the nth poll
opens), `said` (the first sentence containing this text is heard; fragile, as the
teacher's wording changes), each with an optional `after` (seconds). `chat`, `vote` (option
number from 0) and `react` (up, down, done) are what a student does. `every` on a
pause or poll trigger repeats the action for every student (s1, s2 ...), with
`spread` seconds between them.

The transcript is written to logs/sims/, as Markdown, with the students'
descriptions. It holds the pretend students' words. A scenario built from a real
class (`from_record.py`) carries what the real students typed, and so do its
transcripts: keep them as private as the record they came from.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent" / "src"))
load_dotenv(ROOT / "agent" / ".env.local")

from lecturer.models.decisions import make_decisions  # noqa: E402
from lecturer.models.writer_planner import Planner, Writer  # noqa: E402
from lecturer.room.events import ClassEvents  # noqa: E402
from lecturer.room.polls import Polls  # noqa: E402
from lecturer.script.lesson import load_prep  # noqa: E402
from lecturer.settings import model_for  # noqa: E402
from lecturer.speech.decider import VoiceDecider  # noqa: E402
from lecturer.speech.voice import Played  # noqa: E402
from lecturer.teaching.teacher import Teacher, clock  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from students import SimStudent, describe_board, load_personas, summary  # noqa: E402

WORDS_PER_SECOND = 2.6
STUDENTS_EVERY = 15  # outside tasks, model-played students are asked after this many sentences


# -- the pretend room and voice ---------------------------------------------------------


class _Participant:
    async def send_text(self, *_: Any, **__: Any) -> None:
        return None

    def register_rpc_method(self, *_: Any) -> None:
        return None


class FakeRoom:
    def __init__(self) -> None:
        self.local_participant = _Participant()
        self.remote_participants: dict[str, Any] = {}


@dataclass
class SimClip:
    text: str
    ready: bool = True
    duration_ms: float = 0.0
    first_audio_ms: float = 0.0
    error: Exception | None = None

    def cancel(self) -> None:
        return None


class SimVoice:
    def __init__(self, speed: float) -> None:
        self._speed = speed
        self._stop = asyncio.Event()

    def prepare(self, text: str) -> SimClip:
        seconds = len(text.split()) / WORDS_PER_SECOND + 0.3
        return SimClip(text, duration_ms=seconds * 1000)

    async def play(self, clip: SimClip, on_start: Any) -> Played:
        self._stop.clear()
        await on_start()
        try:
            await asyncio.wait_for(self._stop.wait(), clip.duration_ms / 1000 / self._speed)
        except TimeoutError:
            return Played(True, clip.duration_ms)
        return Played(False, 0)

    def stop(self) -> None:
        self._stop.set()


@dataclass
class Vote:
    caller_identity: str
    payload: str


# -- the class --------------------------------------------------------------------------


@dataclass
class Sim:
    scenario: dict[str, Any]
    speed: float
    planner: bool
    lines: list[str] = field(default_factory=list)
    pauses: int = 0
    polls: int = 0
    began: float = 0.0
    fired: set[int] = field(default_factory=set)
    sim_students: list[SimStudent] = field(default_factory=list)
    heard_texts: list[str] = field(default_factory=list)
    chat_log: list[str] = field(default_factory=list)
    since_turn: int = 0  # sentences heard since the students were last asked

    def t(self) -> float:
        return (time.monotonic() - self.began) * self.speed

    def out(self, text: str) -> None:
        line = f"`{clock(self.t())}` {text}"
        self.lines.append(line)
        print(line, flush=True)

    async def run(self) -> Path:
        sc = self.scenario
        prep = load_prep(sc["lesson"])
        events = ClassEvents(FakeRoom())  # type: ignore[arg-type]
        decider = VoiceDecider(
            SimVoice(self.speed), events, pause_scale=1 / self.speed, watch_scale=1 / self.speed
        )  # type: ignore[arg-type]
        polls = Polls(FakeRoom(), events)  # type: ignore[arg-type]
        decider.polls = polls
        students = [f"s{n}" for n in range(1, int(sc.get("students", 5)) + 1)]
        writer_model = model_for("writer")
        if writer_model is None:
            sys.exit("no writer model is set: put CH_WRITER in agent/.env.local")
        planner_model = model_for("planner") if self.planner else None
        teacher = Teacher(
            prep,
            decider,
            events,
            Writer(writer_model),
            students=lambda: students,
            decisions=make_decisions(),
            planner=Planner(planner_model) if planner_model is not None else None,
            minutes=sc.get("minutes"),
            start_at=sc.get("start_at", ""),
            time_scale=self.speed,
        )
        for s in students:
            teacher._handle(s)  # so that s1 is shown as s1
        personas = load_personas(sc)
        if personas:
            student_model = model_for("students")
            if student_model is None:
                sys.exit("the scenario has personas: put CH_STUDENTS in agent/.env.local")
            self.sim_students = [
                SimStudent(h, text, student_model) for h, text in personas.items() if h in students
            ]
        self.events = events

        def heard(what: str, utt: Any, info: dict[str, Any]) -> None:
            if what == "start" and utt.text:
                steps = [self.describe(step) for step in utt.board]
                extra = f"  \n    _{'; '.join(s for s in steps if s)}_" if steps else ""
                self.out(f"**{utt.source}:** {utt.text}{extra}")
                self.trigger("said", text=utt.text)
                self.heard_texts.append(utt.text)
                self.since_turn += 1
                if self.since_turn >= STUDENTS_EVERY:
                    self.students_turn(
                        "The teacher is talking, and has not set a task. Most of the time "
                        "a student types nothing now, unless they have a question or are lost.",
                        20,
                    )
            elif what == "start" and utt.board:
                self.out("_" + "; ".join(self.describe(s) for s in utt.board) + "_")
            elif what == "pause":
                self.pauses += 1
                self.out(f"⏸ pause {utt.pause:g} s: {utt.pause_text}")
                self.trigger("pause", n=self.pauses)
                if not utt.watching:
                    self.students_turn(
                        f'The teacher has set the class a task: "{utt.pause_text}". '
                        f"The class has {utt.pause:g} seconds for it.",
                        utt.pause,
                    )
            elif what == "pause.end":
                self.out(
                    f"▶ pause over after {info['waited'] * self.speed:.0f} s"
                    f"{' (early)' if info['early'] else ''}"
                )

        decider.listeners.append(heard)
        original_open = polls.open

        async def open_poll(question: str, options: list[str]) -> None:
            await original_open(question, options)
            self.polls += 1
            self.out(f"📊 poll: {question} ({' / '.join(options)})")
            self.trigger("poll", n=self.polls)

        polls.open = open_poll  # type: ignore[method-assign]

        self.teacher, self.polls_obj = teacher, polls
        self.students = students
        self.began = time.monotonic()
        voice_task = asyncio.create_task(decider.run())
        clock_task = asyncio.create_task(self.tick())
        watch = asyncio.create_task(self.watch(teacher, sc.get("stop_after", "")))
        lesson = asyncio.create_task(teacher.run())
        await asyncio.wait({lesson, watch}, return_when=asyncio.FIRST_COMPLETED)
        for task in (lesson, watch, voice_task, clock_task):
            task.cancel()
        return self.write(teacher)

    def describe(self, step: Any) -> str:
        if step.verb in ("write", "note"):
            return f"{step.verb} {step.id}: {step.text}"
        if step.verb == "mark":
            return f"mark {step.style} {step.id}{'.' + step.part if step.part else ''}"
        if step.verb in ("title", "page", "slides", "slide"):
            return f"{step.verb}: {step.text}"
        if step.verb == "show":
            return "show: " + " / ".join(" | ".join(p["source"] for p in r) for r in step.rows)
        if step.verb == "poll":
            return "poll close" if step.style == "close" else ""
        return step.verb

    def students_turn(self, now: str, seconds: float) -> None:
        """Ask each model-played student what they do now."""
        self.since_turn = 0
        board = describe_board(self.events.board)
        for student in self.sim_students:
            asyncio.get_running_loop().create_task(self.student_acts(student, now, seconds, board))

    async def student_acts(self, student: SimStudent, now: str, seconds: float, board: str) -> None:
        now = f"{clock(self.t())} into the class. {now}"
        actions = await student.decide(now, self.heard_texts, board, self.chat_log)
        began = time.monotonic()
        for action in sorted(actions, key=lambda a: a.after):
            if action.after > seconds + 20:
                continue
            wait = action.after / self.speed - (time.monotonic() - began)
            if wait > 0:
                await asyncio.sleep(wait)
            if action.chat:
                student.typed.append(action.chat)
                self.chat_log.append(f"{student.handle}: {action.chat}")
                self.teacher.on_chat(student.handle, action.chat)
            if action.done:
                self.out(f"👍 {student.handle} reacts done")
                self.teacher.on_reaction(student.handle, "done")

    async def tick(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            self.trigger("at", now=self.t())

    async def watch(self, teacher: Teacher, stop_after: str) -> None:
        seen = 0
        while True:
            await asyncio.sleep(0.2)
            for entry in list(teacher.trace)[seen:]:
                seen += 1
                kind = entry["kind"]
                if kind == "why":
                    self.out(f"> *why:* {entry['text']}")
                elif kind == "work":
                    self.out(f"> *work:* {entry['text']}")
                elif kind == "sorted":
                    p = ", ".join("-" if x is None else f"{x:.2f}" for x in entry["p"])
                    self.out(f'💬 {entry["who"]}: "{entry["text"]}" → {entry["as_"]} ({p})')
                elif kind == "asked":
                    self.out(f"✎ writer asked: {entry['reason']}")
                elif kind == "wrote":
                    ms = entry["first_line_ms"]
                    self.out(
                        f"✎ wrote {len(entry['lines'])} lines"
                        + (f", first after {ms / 1000:.1f} s" if ms else "")
                    )
                elif kind == "plan":
                    self.out(
                        f"🗺 plan ({'changed' if entry['changed'] else 'same'}): "
                        f"{entry['why']}\n\n{entry['plan']}\n"
                    )
                elif kind in ("failed", "plan failed", "script", "stopped", "bad line"):
                    self.out(f"⚠ {kind}: {entry.get('error') or entry.get('line') or entry}")
                elif kind == "at":
                    self.out(f"📍 part {entry['part']}")
                    self.trigger("part", text=entry["part"])
                    if stop_after and entry["part"] == stop_after:
                        await asyncio.sleep(1)
                        return

    def trigger(self, kind: str, *, n: int = 0, text: str = "", now: float = 0.0) -> None:
        for index, ev in enumerate(self.scenario.get("events", [])):
            if index in self.fired:
                continue
            when = ev["when"]
            hit = (
                (kind == "at" and "at" in when and now >= when["at"])
                or (kind == "pause" and when.get("pause") == n)
                or (kind == "poll" and when.get("poll") == n)
                or (kind == "said" and "said" in when and when["said"].lower() in text.lower())
                or (kind == "part" and str(when.get("part", "")) == text)
            )
            if hit:
                self.fired.add(index)
                asyncio.get_running_loop().create_task(self.act(ev, float(when.get("after", 0))))

    async def act(self, ev: dict[str, Any], after: float) -> None:
        await asyncio.sleep(after / self.speed)
        who_all = self.students if ev.get("every") else None
        spread = float(ev.get("spread", 1.5))
        for n, who in enumerate(who_all or [None]):
            if n:
                await asyncio.sleep(spread / self.speed)
            if "chat" in ev:
                student, text = ev["chat"]
                student = who or student
                self.chat_log.append(f"{student}: {text}")
                self.teacher.on_chat(student, text)
            if "react" in ev:
                student, kind = ev["react"]
                student = who or student
                self.out(f"👍 {student} reacts {kind}")
                self.teacher.on_reaction(student, kind)
            if "vote" in ev:
                student, option = ev["vote"]
                student = who or student
                payload = json.dumps({"poll": self.polls_obj._id, "option": option})
                result = await self.polls_obj._on_vote(Vote(student, payload))  # type: ignore[arg-type]
                self.out(f"🗳 {student} votes option {option} ({result})")

    def measures(self, teacher: Teacher) -> dict[str, Any]:
        """Numbers for comparing runs. What they mean is in `compare.py`."""
        waits = teacher._record
        trace = list(teacher.trace)
        parts = [e["part"] for e in trace if e["kind"] == "at"]
        answers = [a for w in waits for a in w.answers.values()]
        return {
            "minutes": round(self.t() / 60, 1),
            "slot": round(teacher._length / 60, 1),
            "parts": list(dict.fromkeys(parts)),
            "ended": teacher._ended,
            "waits": len(waits),
            "wait_minutes": round(sum(w.waited for w in waits) / 60, 1),
            "waits_ended_early": sum(1 for w in waits if w.early and not w.replaced),
            "waits_cut_by_teacher": sum(1 for w in waits if w.replaced),
            "answers": len(answers),
            "answers_right": sum(1 for a in answers if a.right),
            "chat": sum(1 for e in trace if e["kind"] == "chat"),
            "sentences": sum(1 for e in trace if e["kind"] == "heard"),
            "bad_lines": sum(1 for e in trace if e["kind"] == "bad line"),
            "script_fallbacks": sum(1 for e in trace if e["kind"] == "script"),
            "plans_changed": sum(1 for e in trace if e["kind"] == "plan" and e["changed"]),
            "use": teacher.use(),
            "settings": {
                "writer": os.environ.get("CH_WRITER", ""),
                "planner": self.planner,
                "decisions": os.environ.get("CH_DECISIONS", "") if teacher._decisions else "off",
            },
        }

    def write(self, teacher: Teacher) -> Path:
        name = Path(self.args_scenario).stem
        stamp = time.strftime("%Y%m%d-%H%M%S")
        folder = ROOT / "logs" / "sims"
        folder.mkdir(parents=True, exist_ok=True)
        label = os.environ.get("CH_SIM_LABEL", "") or ("" if self.planner else "noplanner")
        path = folder / f"{name}{'-' + label if label else ''}-{stamp}.md"
        sc = self.scenario
        head = [
            f"# Simulated class: {name}",
            "",
            f"{sc.get('note', '')}",
            "",
            f"Lesson `{sc['lesson']}`, from part {sc.get('start_at') or 'the start'}, "
            f"{sc.get('students', 5)} students, slot {sc.get('minutes') or 'as in the script'} "
            f"min, planner {'on' if self.planner else 'off'}, speed x{self.speed:g}.",
            "",
            f"Model use: `{json.dumps(teacher.use())}`",
            *(
                [f"Students: `{json.dumps(summary(self.sim_students))}`"]
                if self.sim_students
                else []
            ),
            *[f"- {s.handle}: {s.persona}" for s in self.sim_students],
            "",
        ]
        path.write_text("\n".join(head + [line + "  " for line in self.lines]) + "\n")
        path.with_suffix(".json").write_text(json.dumps(self.measures(teacher), indent=2) + "\n")
        return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario")
    parser.add_argument("--speed", type=float, default=2.0, help="class time per real second")
    parser.add_argument("--no-planner", action="store_true")
    args = parser.parse_args()
    sim = Sim(json.loads(Path(args.scenario).read_text()), args.speed, not args.no_planner)
    sim.args_scenario = args.scenario  # type: ignore[attr-defined]
    path = asyncio.run(sim.run())
    print(f"\nwritten to {path}")


if __name__ == "__main__":
    main()
