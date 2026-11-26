"""A check of pictures and video, run with scripted students.

A student sits through the `test/media` test class and reports what the class was
sent: a picture from lesson prep on the board, a picture made during the class, a
video that is loaded, watched, talked over and stopped, a video on a slide, and a
picture in an area of a slide. A second student joins part-way and must be told
which video is loaded.

    cd agent && PYTHONPATH=../dev/checks uv run python ../dev/checks/media_check.py

The made picture needs an image model (CH_IMAGES). Without one the lecturer leaves
the picture out, and that check is reported as skipped. A scripted student cannot
watch a video: whether it plays in a browser is checked by eye.

Prints a JSON summary. Exit code 0 means every check passed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from typing import Any

from student_bot import Student, ensure_lecturer

CLIP = {"kind": "file", "id": "test/test-clip.mp4", "form": ""}


async def main() -> int:
    room = sys.argv[1] if len(sys.argv) > 1 else f"media-{int(time.time()) % 10000}"
    await ensure_lecturer(room, {"lesson": "test/media", "start_delay": 1})
    ada = Student("Ada")
    await ada.join(room)
    late = Student("Late")

    async def join_late() -> None:
        await asyncio.sleep(22)
        await late.join(room)

    joining = asyncio.create_task(join_late())
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(ada.ended.wait(), timeout=180)
    await asyncio.sleep(0.5)
    joining.cancel()

    def of(kind: str) -> list[dict[str, Any]]:
        return [e for e in ada.seen if e["type"] == kind]

    checks: dict[str, bool] = {}
    notes: dict[str, Any] = {"room": room}

    pictures = [e for e in of("board.write") if e.get("kind") == "image"]
    checks["picture_from_prep_on_the_board"] = any(
        e["content"] == "lesson-files/test/test-card.svg" for e in pictures
    )
    checks["the_picture_was_marked"] = any(e.get("id") == "card" for e in of("board.mark"))

    asked = [e for e in pictures if e.get("pending")]
    filled = of("board.fill")
    if not asked:
        notes["made_picture"] = "skipped: the lecturer has no image model"
    else:
        notes["made_picture"] = filled[0].get("content") if filled else None
        notes["seconds_to_make_it"] = (
            round(filled[0]["got"] - asked[0]["got"], 1) if filled else None
        )
        checks["made_picture_filled_its_place"] = bool(
            filled
            and filled[0]["content"].startswith("class-files/")
            and not filled[0].get("failed")
        )

    plays, stops = of("video.play"), of("video.stop")
    checks["video_loaded"] = bool(of("video.load")) and of("video.load")[0]["src"] == CLIP
    watching = [e for e in of("pause") if e.get("watching")]
    checks["class_was_given_time_to_watch"] = len(watching) >= 2
    first = next((e for e in plays if not e.get("quiet")), None)
    first_stop = next((e for e in stops if first and e["got"] > first["got"]), None)
    watched = round(first_stop["got"] - first["got"], 1) if first and first_stop else None
    notes["seconds_watched"] = watched
    checks["played_for_the_time_given_then_stopped"] = watched is not None and 3 <= watched <= 6
    notes["moment_it_was_stopped_at"] = first_stop["position"] if first_stop else None
    over = next((e for e in plays if e.get("quiet")), None)
    checks["played_from_a_moment_while_talking"] = bool(over) and over["position"] == 6  # type: ignore[index]
    checks["a_slide_holds_the_video"] = any(
        item["kind"] == "video" and item["video"] == CLIP
        for e in of("slides.load")
        for slide in e["deck"]["slides"]
        for item in slide["items"]
    )
    checks["picture_in_an_area_of_a_slide"] = any(
        e.get("kind") == "image" and e.get("area") == "work" for e in of("slides.write")
    )
    checks["video_not_left_playing"] = not ada.stage.video["playing"]
    checks["late_joiner_told_which_video"] = bool(
        late.snapshot and late.snapshot["stage"]["video"]["src"] == CLIP
    )
    notes["late_joiner_video"] = late.snapshot["stage"]["video"] if late.snapshot else None
    checks["late_joiner_ends_the_same"] = late.stage.snapshot() == ada.stage.snapshot()
    checks["class_ended"] = ada.phase == "ended"
    checks["no_gaps_in_sequence"] = ada.gaps == 0

    print(json.dumps({**notes, "checks": checks}, indent=2, ensure_ascii=False))
    for student in (ada, late):
        with contextlib.suppress(Exception):
            await student.room.disconnect()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
