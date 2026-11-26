"""Run one simulated class several ways and compare: does the planner help? Does
the decision model? Which writer model teaches better?

    cd agent && uv run python ../dev/eval/compare.py <scenario.json> \\
        --variants full,no-planner,no-decisions --runs 2 --speed 3 --judge

Variants (more can be added to VARIANTS below):
    full           the models as set in agent/.env.local
    no-planner     without the planner
    no-decisions   without the decision model (the teacher sees every message)
    neither        without either
    writer=<provider:model>   another writer model, the rest as set

Each run is a whole simulated class (`sim_class.py`), so each costs what a class
costs. Runs of the same variant differ, as classes do: compare several, not one.

The numbers for each run (from the `.json` beside its transcript):
    minutes         how long it ran, against the slot
    parts           the parts of the lesson it reached
    waits           tasks and pauses set, and the minutes the class spent on them
    early / cut     waits that ended because everyone had responded / that the
                    teacher cut short by speaking
    answers, right  answers typed during waits, and how many reached the right answer
    bad lines       lines the teacher wrote that could not be used
    fallbacks       times the script was read because the writer failed

With --judge, a model (CH_PLANNER's) reads each transcript and scores it against
the rubric below, with a reason for each score. It is one model's reading, and it
knows nothing of the other runs; it is not a substitute for a person watching.

The report goes to logs/sims/compare-<time>.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent" / "src"))
load_dotenv(ROOT / "agent" / ".env.local")

from lecturer.models.providers import parse_json  # noqa: E402
from lecturer.settings import model_for  # noqa: E402

VARIANTS: dict[str, tuple[dict[str, str], list[str]]] = {
    "full": ({}, []),
    "no-planner": ({}, ["--no-planner"]),
    "no-decisions": ({"CH_DECISIONS": "off"}, []),
    "neither": ({"CH_DECISIONS": "off"}, ["--no-planner"]),
}

RUBRIC = """You are reading the transcript of a simulated live class taught by an AI
teacher, to judge how well it taught. The students were played by a model.

Score each of these from 1 (bad) to 5 (excellent), with one sentence of reason
that points at what happened in the transcript:

- pace: explains slowly and fully, says every step of working; does not speed up,
  skip steps or cut explanation because the class is quiet.
- thinking_time: gives the class real time to think on problems, for everyone (not
  ended by one student), asks everyone to work it out, then asks what they found.
- step_by_step: goes through problems a step at a time, asking the class for each
  step, and builds on what students typed.
- responding: notices what students type, corrects mistakes kindly, answers
  questions well, never names students, and does not let one student take over.
- correct: everything said and written is mathematically right, including any
  problem a student brought that was not prepared.
- time: fits the slot, covers what must be covered, and ends properly.
- overall: how good a class this was for these students.

Reply with JSON only:
{"pace": {"score": n, "why": "..."}, "thinking_time": {...}, "step_by_step": {...},
 "responding": {...}, "correct": {...}, "time": {...}, "overall": {...}}"""

SCORES = ("pace", "thinking_time", "step_by_step", "responding", "correct", "time", "overall")


def variant(name: str) -> tuple[dict[str, str], list[str]]:
    if name.startswith("writer="):
        return {"CH_WRITER": name.partition("=")[2]}, []
    if name not in VARIANTS:
        sys.exit(f"no variant called {name!r}: {', '.join(VARIANTS)} or writer=<model>")
    return VARIANTS[name]


def run(scenario: str, name: str, n: int, speed: float) -> Path | None:
    env_over, flags = variant(name)
    label = f"{name.replace('=', '-').replace(':', '-').replace('/', '-')}-r{n}"
    env = {**os.environ, **env_over, "CH_SIM_LABEL": label}
    cmd = ["uv", "run", "python", str(ROOT / "dev/eval/sim_class.py"), scenario]
    cmd += ["--speed", str(speed), *flags]
    print(f"running {label} ...", flush=True)
    done = subprocess.run(cmd, cwd=ROOT / "agent", env=env, capture_output=True, text=True)
    for line in reversed(done.stdout.splitlines()):
        if line.startswith("written to "):
            return Path(line.removeprefix("written to ").strip())
    print(f"{label} failed:\n{done.stderr[-2000:]}", flush=True)
    return None


async def judge(transcript: Path) -> dict[str, Any]:
    model = model_for("planner") or model_for("writer")
    if model is None:
        return {}
    reply = await model.complete(
        RUBRIC,
        [{"role": "user", "content": transcript.read_text()[:180_000]}],
        max_tokens=1500,
        retries=1,
    )
    try:
        return parse_json(reply.text)
    except ValueError:
        return {"error": reply.text[:500]}


def row(name: str, m: dict[str, Any], scores: dict[str, Any]) -> str:
    cells = [
        name,
        f"{m['minutes']}/{m['slot']}",
        ",".join(m["parts"]),
        f"{m['waits']} ({m['wait_minutes']} min)",
        f"{m['waits_ended_early']}/{m['waits_cut_by_teacher']}",
        f"{m['answers']}/{m['answers_right']}",
        str(m["bad_lines"]),
        str(m["script_fallbacks"]),
    ]
    cells += [str(scores.get(k, {}).get("score", "")) for k in SCORES] if scores else []
    return "| " + " | ".join(cells) + " |"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--variants", default="full,no-planner")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--speed", type=float, default=3.0)
    ap.add_argument("--judge", action="store_true")
    args = ap.parse_args()
    names = [v.strip() for v in args.variants.split(",") if v.strip()]
    for name in names:
        variant(name)  # fail now, not after an hour of runs
    results = []
    for name in names:
        for n in range(1, args.runs + 1):
            path = run(args.scenario, name, n, args.speed)
            if path is None:
                continue
            measures = json.loads(path.with_suffix(".json").read_text())
            scores = await judge(path) if args.judge else {}
            results.append((f"{name} #{n}", path, measures, scores))
    head = ["run", "minutes", "parts", "waits", "early/cut", "answers/right", "bad", "fallback"]
    if args.judge:
        head += list(SCORES)
    lines = [
        f"# Comparison: {Path(args.scenario).stem}",
        "",
        "| " + " | ".join(head) + " |",
        "|" + "---|" * len(head),
    ]
    lines += [row(name, m, s) for name, _, m, s in results]
    lines.append("")
    for name, path, m, s in results:
        lines += [
            f"## {name}",
            "",
            f"Transcript: `{path}`",
            "",
            f"Model use: `{json.dumps(m['use'])}`",
        ]
        for key in SCORES:
            if key in s:
                lines.append(f"- {key}: {s[key].get('score')}: {s[key].get('why', '')}")
        lines.append("")
    out = ROOT / "logs" / "sims" / f"compare-{time.strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text("\n".join(lines) + "\n")
    print("\n".join(lines[: 4 + len(results)]))
    print(f"\nwritten to {out}")


if __name__ == "__main__":
    asyncio.run(main())
