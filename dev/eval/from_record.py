"""A simulated class built from a real one: the same lesson, and pretend students
who act like the real students did.

A real class taught live with CH_RECORD_CLASS=1 leaves a record in logs/recorded/:
everything the teacher saw and did, students' words included. From it, each
student is written up for a model to play (`students.py`): their level, how often
and how they type, their mistakes, what they asked about, with what they actually
did, moment by moment, as examples. The simulated class can then be run again and
again, with the teacher set up in different ways, to see what changes.

    cd agent && uv run python ../dev/eval/from_record.py <record.jsonl> <course>/<lesson> \\
        --minutes 45 --out <scenario.json>

The model that writes the students up is CH_PLANNER's (a strong model is better
here). The scenario holds the students' words, so keep it with the lesson, out of
the repo.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent" / "src"))
load_dotenv(ROOT / "agent" / ".env.local")

from lecturer.settings import model_for  # noqa: E402
from lecturer.teaching.teacher import clock  # noqa: E402

WRITE_UP = """You are helping test an AI teacher. Below is what one student did in a real
live online class: each thing they typed or pressed, when, and what the teacher had
just said or asked. Write this student up so that another model can play them in a
simulated class and act as they would, even where the teacher does something
different.

Say, in plain sentences: how much of the subject they seemed to know; how often
they typed (how many tasks they answered, and how many they left); how they wrote
(bare numbers, full working, questions, little words like "?"); the mistakes they
made and where they got stuck; what they asked about or wanted more of; how they
reacted to being waited for. Keep to what the record shows: do not invent a life
story. Then end with three to six of their messages, word for word, as examples.

Under 250 words. Write it to the student ("You are ..."), as the description of
who they are."""


def moments(record: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Each student's actions, with what was going on, as lines of text."""
    task = ""
    said: list[str] = []
    out: dict[str, list[str]] = {}
    working = False
    for entry in record:
        kind = entry.get("kind")
        if kind == "heard":
            said.append(entry.get("text", ""))
        elif kind == "pause":
            task, working = entry.get("text", ""), True
        elif kind == "pause.end":
            working = False
        elif kind in ("chat", "reaction"):
            who = entry.get("who", "?")
            what = (
                f'typed "{entry.get("text", "")}"'
                if kind == "chat"
                else f"pressed {entry.get('reaction')}"
            )
            last = said[-1] if said else ""
            when = (
                f'while the class was working on "{task}"'
                if working
                else f'with no task open (the last one was "{task or "none yet"}")'
            )
            out.setdefault(who, []).append(
                f"- At {clock(float(entry.get('t', 0)))}, {what}, {when}. "
                f'The teacher had just said: "{last}"'
            )
    return out


async def write_up(lines: list[str]) -> str:
    model = model_for("planner") or model_for("writer")
    if model is None:
        sys.exit("no model to write the students up: set CH_PLANNER or CH_WRITER")
    reply = await model.complete(
        WRITE_UP, [{"role": "user", "content": "\n".join(lines)}], max_tokens=900, retries=1
    )
    return reply.text.strip()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("record")
    ap.add_argument("lesson")
    ap.add_argument("--minutes", type=float, default=0)
    ap.add_argument("--students", type=int, default=0, help="if more were there than typed")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    record = [json.loads(ln) for ln in Path(args.record).read_text().splitlines() if ln.strip()]
    by_student = moments(record)
    if not by_student:
        sys.exit("nobody typed or pressed anything in this record")
    personas: dict[str, str] = {}
    for who, lines in sorted(by_student.items()):
        about = await write_up(lines)
        personas[who] = (
            f"{about}\n\nWhat you actually did in the real class, for reference (the "
            "teacher may do things differently this time; act as you would):\n" + "\n".join(lines)
        )
        print(f"--- {who}\n{about}\n", flush=True)
    scenario = {
        "lesson": args.lesson,
        "students": max(args.students, len(personas)),
        "note": f"Built from the record {Path(args.record).name}.",
        "personas": personas,
        "events": [],
    }
    if args.minutes:
        scenario["minutes"] = args.minutes
    Path(args.out).write_text(json.dumps(scenario, indent=2) + "\n")
    print(f"written to {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
