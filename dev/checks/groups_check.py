"""A check of the class splitting into groups, run with scripted people.

Two students and two helpers join the `test/groups` test class. It splits into two
groups, each a room of its own with its own lecturer reading a short test lesson. The
students each go to one, with a helper, so the main room is left empty. Each group's
lecturer sends its students back at the end, and the main lecturer, which has waited,
carries on from where it was.

Then a second class is run with no helpers. It must not split.

    cd agent && PYTHONPATH=../dev/checks uv run python ../dev/checks/groups_check.py

Prints a JSON summary. Exit code 0 means every check passed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from typing import Any

from floor_check import until
from student_bot import Student, ensure_lecturer

LAST = "That is the end of the test."


def of(person: Student, kind: str) -> list[dict[str, Any]]:
    return [e for e in person.seen if e["type"] == kind]


def heard(person: Student, text: str) -> bool:
    return any(text in e.get("text", "") for e in of(person, "say"))


async def leave(*people: Student) -> None:
    for person in people:
        with contextlib.suppress(Exception):
            await person.room.disconnect()


async def with_helpers(room: str, checks: dict[str, bool], notes: dict[str, Any]) -> None:
    await ensure_lecturer(room, {"lesson": "test/groups", "start_delay": 2})
    ada, bo = Student("Ada"), Student("Bo")
    hana, hugo = Student("Hana", "helper"), Student("Hugo", "helper")
    for person in (hana, hugo, ada, bo):
        await person.join(room)

    checks["class_split"] = await until(lambda: bool(of(ada, "split.open")), 40)
    if not checks["class_split"]:
        await leave(ada, bo, hana, hugo)
        return
    opened = of(ada, "split.open")[0]
    first, second = opened["groups"]
    notes["groups"] = [g["title"] for g in opened["groups"]]
    checks["students_choose"] = opened["how"] == "choose" and opened["to"] == {}
    run = (ada.snapshot or {}).get("epoch")
    split_at = time.monotonic()
    await leave(ada, bo, hana, hugo)  # the main room is now empty

    # Each student goes to a group, with a helper.
    ada_there, hana_there = Student("Ada"), Student("Hana", "helper")
    bo_there, hugo_there = Student("Bo"), Student("Hugo", "helper")
    await asyncio.gather(
        ada_there.join(first["room"]),
        hana_there.join(first["room"]),
        bo_there.join(second["room"]),
        hugo_there.join(second["room"]),
    )
    checks["each_group_has_a_lecturer"] = await until(
        lambda: bool(ada_there.snapshot and bo_there.snapshot), 30
    )
    checks["a_group_says_which_it_is"] = bool(
        ada_there.snapshot
        and (ada_there.snapshot.get("group") or {}).get("title") == first["title"]
        and (ada_there.snapshot.get("group") or {}).get("main") == room
    )
    checks["groups_teach_their_own_lessons"] = await until(
        lambda: heard(ada_there, "first page") and heard(bo_there, "first problem"), 40
    )
    checks["students_sent_back_at_the_end"] = await until(
        lambda: bool(of(ada_there, "split.back")) and bool(of(bo_there, "split.back")), 90
    )
    back = of(ada_there, "split.back")
    checks["sent_back_to_the_main_class"] = bool(back) and back[0]["room"] == room
    notes["seconds_the_main_room_was_empty"] = round(time.monotonic() - split_at)
    await leave(ada_there, bo_there, hana_there, hugo_there)

    # Back in the main room: the same lecturer, which waited, carries on.
    ada_back, bo_back = Student("Ada"), Student("Bo")
    await ada_back.join(room)
    await bo_back.join(room)
    checks["main_lecturer_was_still_there"] = await until(lambda: ada_back.snapshot is not None, 20)
    checks["it_is_the_same_lecturer"] = bool(run) and (ada_back.snapshot or {}).get("epoch") == run
    checks["main_class_carries_on"] = await until(lambda: heard(ada_back, "Everyone is back"), 60)
    checks["groups_are_over"] = await until(
        lambda: bool(of(ada_back, "split.close")) or (ada_back.snapshot or {}).get("split") is None,
        30,
    )
    checks["it_did_not_start_again"] = not heard(ada_back, "about to split")
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(ada_back.ended.wait(), timeout=30)
    checks["class_ended"] = ada_back.phase == "ended"
    await leave(ada_back, bo_back)


async def without_helpers(room: str, checks: dict[str, bool]) -> None:
    await ensure_lecturer(room, {"lesson": "test/groups", "start_delay": 1})
    ada = Student("Ada")
    await ada.join(room)
    checks["no_helpers_class_goes_on_together"] = await until(lambda: heard(ada, LAST), 40)
    checks["no_helpers_no_split"] = not of(ada, "split.open")
    await leave(ada)


async def main() -> int:
    room = sys.argv[1] if len(sys.argv) > 1 else f"groups-{int(time.time()) % 10000}"
    checks: dict[str, bool] = {}
    notes: dict[str, Any] = {"room": room}
    await with_helpers(room, checks, notes)
    await without_helpers(f"{room}-alone", checks)
    print(json.dumps({**notes, "checks": checks}, indent=2, ensure_ascii=False))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
