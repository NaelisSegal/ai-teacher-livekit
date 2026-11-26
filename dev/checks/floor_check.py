"""A check of who may speak, run with scripted people.

Two students and a helper join the `test/floor` test class and do what real people
would: try to talk when not allowed, raise a hand, talk when given the floor, talk
when the mics are opened, and (the helper) talk over the lecturer and share a
screen. "Talking" is a tone, which LiveKit treats as a voice.

    cd agent && PYTHONPATH=../dev/checks uv run python ../dev/checks/floor_check.py floor-1

Prints a JSON summary. Exit code 0 means every check passed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import sys
import time
from collections.abc import Callable
from typing import Any

import numpy as np
from livekit import rtc
from student_bot import Student, ensure_lecturer, show_text

RATE = 48000


async def until(what: Callable[[], bool], seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if what():
            return True
        await asyncio.sleep(0.05)
    return what()


async def speak(person: Student, seconds: float) -> bool:
    """Publish a microphone and make sound for a while. False if LiveKit refuses."""
    source = rtc.AudioSource(RATE, 1)
    track = rtc.LocalAudioTrack.create_audio_track("mic", source)
    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    try:
        publication = await asyncio.wait_for(
            person.room.local_participant.publish_track(track, options), timeout=5
        )
    except Exception:
        return False
    samples = RATE // 100
    for n in range(int(seconds * 100)):
        t = (np.arange(samples) + n * samples) / RATE
        wave = (0.3 * np.sin(2 * math.pi * 330 * t) * 32767).astype(np.int16)
        await source.capture_frame(rtc.AudioFrame(wave.tobytes(), RATE, 1, samples))
    await asyncio.sleep(0.2)
    with contextlib.suppress(Exception):
        await person.room.local_participant.unpublish_track(publication.sid)
    return True


async def share_screen(person: Student, seconds: float) -> bool:
    source = rtc.VideoSource(640, 360)
    track = rtc.LocalVideoTrack.create_video_track("screen", source)
    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_SCREENSHARE)
    try:
        publication = await asyncio.wait_for(
            person.room.local_participant.publish_track(track, options), timeout=5
        )
    except Exception:
        return False
    picture = bytes([40, 90, 200, 255]) * (640 * 360)
    for _ in range(int(seconds * 5)):
        source.capture_frame(rtc.VideoFrame(640, 360, rtc.VideoBufferType.RGBA, picture))
        await asyncio.sleep(0.2)
    await person.room.local_participant.unpublish_track(publication.sid)
    return True


async def ask(person: Student, method: str, payload: str) -> str:
    lecturer = person._lecturer()
    assert lecturer, "no lecturer in the room"
    return await person.room.local_participant.perform_rpc(
        destination_identity=lecturer.identity, method=method, payload=payload
    )


def says(person: Student, start: float, end: float) -> list[str]:
    """Lesson sentences (not ready lines) the lecturer began in this stretch of time."""
    return [
        e["text"]
        for e in person.seen
        if e["type"] == "say"
        and e.get("source") not in ("ready", "invite")
        and start < e["got"] < end
    ]


async def main() -> int:
    room = sys.argv[1] if len(sys.argv) > 1 else f"floor-{int(time.time()) % 10000}"
    await ensure_lecturer(room, {"lesson": "test/floor", "start_delay": 1})
    ada, ben = Student("Ada"), Student("Ben")
    await ada.join(room)
    await ben.join(room)
    checks: dict[str, bool] = {}
    notes: dict[str, Any] = {"room": room}

    await until(lambda: ada.phase == "teaching", 20)
    checks["class_started_with_hands"] = ada.floor.mode == "hands"

    # 1. A student cannot simply start talking.
    checks["student_cannot_talk_uninvited"] = not await speak(ada, 0.3)

    # 2. A raised hand gets the floor, after the sentence being spoken.
    raised = time.time()
    await ask(ada, "ch.hand", "up")
    checks["hand_shows_as_raised"] = await until(
        lambda: any(h["name"] == "Ada" for h in ben.floor.hands) or ben.floor.speaker is not None,
        5,
    )
    got_floor = await until(lambda: (ada.floor.speaker or {}).get("name") == "Ada", 20)
    given = time.time()
    checks["hand_was_given_the_floor"] = got_floor
    notes["seconds_from_hand_to_floor"] = round(given - raised, 1)
    invited = [e["text"] for e in ada.seen if e["type"] == "say" and e.get("source") == "invite"]
    checks["lecturer_invited_them_by_name"] = invited == ["Ada, go ahead."]
    checks["could_talk_with_the_floor"] = await speak(ada, 3.0)
    await ask(ada, "ch.done", "")
    checks["done_gives_the_floor_back"] = await until(lambda: ada.floor.speaker is None, 5)
    taken = time.time()
    notes["ben_heard_ada_ms"] = round(ben.heard_people.get("bot-Ada", 0))
    checks["others_heard_them"] = notes["ben_heard_ada_ms"] > 1500
    # Measured to the moment Ben saw the floor taken back. The lecturer carries on
    # in that same instant, so a clock read here afterwards would be too late.
    took = next((e["got"] for e in reversed(ben.seen) if e["type"] == "floor.take"), taken)
    checks["lecturer_was_quiet_during_the_floor"] = says(ben, given + 0.3, took - 0.01) == []
    await asyncio.sleep(1.5)
    checks["mic_taken_back_after_the_floor"] = not await speak(ada, 0.3)

    # 2b. Reactions are of the moment: others see one when it is sent, and it is not
    # class state. Done from every student ends a thinking pause early.
    def last(person: Student, kind: str) -> dict[str, Any]:
        return next((e for e in reversed(person.seen) if e["type"] == kind), {})

    async def react(person: Student, kind: str) -> None:
        await person.room.local_participant.send_text(kind, topic="ch.react")

    # 2a. A poll from the lecturer: one vote each, changeable, totals for everyone.
    checks["lecturer_opened_a_poll"] = await until(lambda: bool(last(ben, "poll.open")), 40)
    poll = last(ben, "poll.open")
    notes["poll"] = [poll.get("question"), *poll.get("options", [])]
    await ask(ada, "ch.vote", json.dumps({"poll": poll["id"], "option": 0}))
    await ask(ben, "ch.vote", json.dumps({"poll": poll["id"], "option": 1}))
    await ask(ben, "ch.vote", json.dumps({"poll": poll["id"], "option": 0}))  # changes mind
    checks["everyone_sees_poll_totals"] = await until(
        lambda: last(ada, "poll.results").get("counts") == [2, 0, 0], 5
    )
    try:
        await ask(ben, "ch.vote", json.dumps({"poll": poll["id"], "option": 9}))
        checks["a_vote_for_no_option_is_refused"] = False
    except rtc.RpcError:
        checks["a_vote_for_no_option_is_refused"] = True

    await react(ada, "up")
    await react(ada, "down")
    checks["others_see_a_reaction"] = await until(
        # Each reaction is its own message, so the two may arrive in either order.
        lambda: sorted(k for who, k in ben.reactions if who == "bot-Ada") == ["down", "up"],
        5,
    )
    checks["reactions_are_not_class_events"] = not any(
        e["type"].startswith("react") for e in ben.seen
    )
    checks["thinking_pause_began"] = await until(lambda: bool(last(ben, "pause")), 40)
    pause_began = last(ben, "pause").get("got", time.time())
    await asyncio.sleep(2)
    await react(ada, "done")
    await asyncio.sleep(1.5)
    not_yet = last(ben, "pause.end").get("got", 0) < pause_began
    await react(ben, "done")
    ended = await until(lambda: last(ben, "pause.end").get("got", 0) > pause_began, 6)
    notes["pause_of_30s_ended_after_s"] = (
        round(last(ben, "pause.end")["got"] - pause_began, 1) if ended else None
    )
    checks["pause_waited_for_everyone"] = not_yet
    checks["pause_ended_early_when_all_done"] = ended

    # 3. The lecturer opens the mics: anyone may speak. Then closes them again.
    checks["poll_was_closed_with_its_totals"] = await until(
        lambda: last(ben, "poll.close").get("counts") == [2, 0, 0], 20
    )
    checks["mics_were_opened"] = await until(lambda: ben.floor.mode == "open", 60)
    notes["prompt_with_open_mics"] = ben.floor.prompt
    await asyncio.sleep(1.0)
    opened = time.time()
    checks["could_talk_with_open_mics"] = await speak(ben, 2.5)
    notes["ada_heard_ben_ms"] = round(ada.heard_people.get("bot-Ben", 0))
    checks["others_heard_open_mic"] = notes["ada_heard_ben_ms"] > 1200
    checks["lecturer_was_quiet_for_open_mic"] = says(ada, opened + 0.5, time.time()) == []
    checks["mics_went_back_to_hands"] = await until(lambda: ben.floor.mode == "hands", 40)
    await asyncio.sleep(1.5)
    checks["mic_taken_back_after_open"] = not await speak(ben, 0.3)

    # 4. A helper may talk at any time, and the lecturer stops for them.
    helper = Student("Hana", role="helper")
    await helper.join(room)
    await until(lambda: helper.synced, 5)
    await until(lambda: any(e["type"] == "say" for e in ada.seen[-3:]), 5)
    cuts_before = sum(1 for e in ada.seen if e["type"] == "cut")
    began = time.time()
    checks["helper_can_talk_any_time"] = await speak(helper, 2.5)
    ended = time.time()
    notes["ada_heard_helper_ms"] = round(ada.heard_people.get("bot-Hana", 0))
    cut = [e for e in ada.seen if e["type"] == "cut"][cuts_before:]
    checks["lecturer_stopped_for_the_helper"] = bool(cut)
    notes["lecturer_stopped_after_ms"] = round((cut[0]["got"] - began) * 1000) if cut else None
    checks["lecturer_stayed_quiet_for_the_helper"] = says(ada, began + 1.0, ended) == []
    texts = [e["text"] for e in ada.seen if e["type"] == "say"]
    await until(lambda: len([e for e in ada.seen if e["type"] == "say"]) > len(texts), 8)
    after = [e["text"] for e in ada.seen if e["type"] == "say"][len(texts) :]
    checks["cut_sentence_was_said_again"] = bool(after) and after[0] in texts

    # 5. A helper's shared screen takes the stage, and the stage comes back after.
    before = show_text(ada.stage.rows)
    checks["helper_can_share_a_screen"] = await share_screen(helper, 3.0)
    shown = [show_text(e["rows"]) for e in ada.seen if e["type"] == "stage.show"]
    notes["stage_during_and_after_share"] = shown[-2:]
    checks["shared_screen_took_the_stage"] = any(s.startswith("share") for s in shown)
    checks["stage_came_back_after_the_share"] = await until(
        lambda: show_text(ada.stage.rows) == before, 5
    )
    checks["a_student_cannot_share_a_screen"] = not await share_screen(ben, 0.4)

    # 6. Only staff can run the room.
    try:
        await ask(ben, "ch.staff", json.dumps({"action": "mode", "mode": "open"}))
        checks["a_student_cannot_open_the_mics"] = False
    except rtc.RpcError:
        checks["a_student_cannot_open_the_mics"] = True

    checks["no_gaps_in_sequence"] = ada.gaps == 0 and ben.gaps == 0
    print(json.dumps({**notes, "checks": checks}, indent=2))
    for person in (ada, ben, helper):
        await person.room.disconnect()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
