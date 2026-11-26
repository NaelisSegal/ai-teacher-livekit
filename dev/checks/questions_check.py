"""A check of chat questions, run with a scripted student.

A student sits through a lesson and types three things: a quick question, a
thank-you, and a question too big for a quick reply. The two questions depend on the
lesson, so they are given with it. The lesson should run for a few minutes. It needs
the lecturer to have models for chat questions (CH_QUESTIONS and CH_ANSWERS), and it
spends a little: three calls to the quick model and one to the stronger.

    cd agent && PYTHONPATH=../dev/checks uv run python ../dev/checks/questions_check.py \
        --lesson <course>/<lesson> \
        --quick "a question the lesson answers in a sentence or two" \
        --big "a question that goes beyond the lesson"

Prints a JSON summary. Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
import time
from typing import Any

from floor_check import until
from student_bot import Student, ensure_lecturer, newest_log

THANKS = "thanks, that makes sense now"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("room", nargs="?", default=f"questions-{int(time.time()) % 10000}")
    ap.add_argument("--lesson", required=True, help="<course>/<lesson>")
    ap.add_argument("--quick", required=True, help="a question with a short answer")
    ap.add_argument("--big", required=True, help="a question too big for a quick reply")
    args = ap.parse_args()
    room = args.room
    await ensure_lecturer(room, {"lesson": args.lesson, "start_delay": 1, "pause_scale": 0.2})
    ada = Student("Ada")
    await ada.join(room)
    checks: dict[str, bool] = {}
    notes: dict[str, Any] = {"room": room}

    async def type_in_chat(text: str) -> float:
        await ada.room.local_participant.send_text(text, topic="ch.chat")
        return time.time()

    def says(source: str, after: float) -> list[dict[str, Any]]:
        return [
            e
            for e in ada.seen
            if e["type"] == "say" and e.get("source") == source and e["got"] > after
        ]

    await until(lambda: ada.phase == "teaching", 20)
    await asyncio.sleep(25)

    # 1. A quick question is answered after the sentence being spoken.
    asked = await type_in_chat(args.quick)
    checks["quick_question_answered"] = await until(lambda: bool(says("answer", asked)), 25)
    answer = says("answer", asked)
    notes["seconds_to_first_word_of_answer"] = (
        round(answer[0]["got"] - asked, 1) if answer else None
    )
    await until(lambda: len(says("answer", asked)) >= 2, 20)
    await asyncio.sleep(6)
    notes["quick_answer"] = [e["text"] for e in says("answer", asked)]
    lesson_after = (
        [e for e in says("script", asked) if e["got"] > answer[-1]["got"]] if answer else []
    )
    checks["lesson_carried_on_after_the_answer"] = await until(
        lambda: bool(
            [
                e
                for e in says("script", asked)
                if answer and e["got"] > says("answer", asked)[-1]["got"]
            ]
        ),
        20,
    ) or bool(lesson_after)

    # 2. A thank-you is left alone.
    answers_so_far = len(says("answer", 0)) + len(says("ready", 0))
    thanked = await type_in_chat(THANKS)
    await asyncio.sleep(12)
    checks["thanks_not_answered"] = len(says("answer", 0)) + len(says("ready", 0)) == answers_so_far
    del thanked

    # 3. A big question is put aside, and answered at the end.
    big = await type_in_chat(args.big)
    checks["big_question_put_aside"] = await until(
        lambda: any("come back" in e["text"] for e in says("ready", big)), 25
    )
    put_aside = [e for e in says("ready", big) if "come back" in e["text"]]
    notes["seconds_to_put_aside"] = round(put_aside[0]["got"] - big, 1) if put_aside else None

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(ada.ended.wait(), timeout=420)
    await asyncio.sleep(0.5)
    last_lesson = max(
        (e["got"] for e in ada.seen if e["type"] == "say" and e.get("source") == "script"),
        default=0,
    )
    kept = says("answer", last_lesson)
    notes["answer_at_the_end"] = [e["text"] for e in kept]
    checks["promise_kept_at_the_end"] = len(kept) >= 3
    checks["questions_page_on_the_board"] = "Questions" in [
        ln["content"] for ln in ada.board.snapshot()["lines"]
    ]
    checks["class_ended"] = ada.phase == "ended"

    # The log has the answers and none of what the student typed.
    log_text = json.dumps(newest_log(room))
    checks["student_words_not_in_log"] = not any(
        q in log_text for q in (args.quick, THANKS, args.big)
    )
    notes["log_notes"] = [
        {k: v for k, v in e.items() if k != "t"}
        for e in newest_log(room)
        if e["type"]
        in ("note.question", "note.answer_began", "note.promise_kept", "note.model_use")
    ]
    checks["no_gaps_in_sequence"] = ada.gaps == 0
    print(json.dumps({**notes, "checks": checks}, indent=2, ensure_ascii=False))
    await ada.room.disconnect()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
