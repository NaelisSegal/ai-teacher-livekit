"""A scripted student, for checks.

It joins a class room like a browser would, asks for a snapshot, listens to the
lecturer's audio, applies class events in order, and reports what it actually
received. It also asks LiveKit to send the lecturer agent to the room, if none has been sent.

    cd agent && uv run python ../dev/checks/student_bot.py check-1 --lesson test/pages

    --lesson <course>/<lesson>   the lesson to sit through. The test lessons beside
                     this file (`lessons/test/`) are found when the class is run by
                     `dev/stack.sh`.
    --pause-scale 0.1   thinking pauses a tenth as long

    --late-join 40   a second student joins 40 s in; its snapshot is compared
                     with what the first student had built up by then
    --seconds 30     leave after 30 s instead of waiting for the lesson to end

Prints a JSON summary. Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import glob
import json
import os
import statistics
import sys
import time
from typing import Any

import numpy as np
from livekit import api, rtc

from lecturer.room.board import Board
from lecturer.room.floor import FloorState
from lecturer.room.stage import Stage

URL = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:4070")
KEY = os.environ.get("LIVEKIT_API_KEY", "devkey")
SECRET = os.environ.get("LIVEKIT_API_SECRET", "secret")
CLASS_TOPIC = "ch.class"
VOICED = 0.01  # RMS above this counts as the lecturer speaking


def token(room: str, identity: str, name: str, role: str = "student") -> str:
    """A join token like the web app's: students cannot publish, staff can."""
    staff = role in ("helper", "guest")
    sources = ["microphone", "screen_share", "screen_share_audio"] if staff else []
    grants = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=staff,
        can_publish_sources=sources,
        can_publish_data=True,
    )
    return (
        api.AccessToken(KEY, SECRET)
        .with_identity(identity)
        .with_name(name)
        .with_attributes({"ch.role": role})
        .with_grants(grants)
        .to_jwt()
    )


async def ensure_lecturer(room: str, meta: dict[str, Any]) -> None:
    """Send the lecturer to the room, unless one has been sent already."""
    async with api.LiveKitAPI(URL.replace("ws", "http", 1), KEY, SECRET) as lk:
        try:
            sent = await lk.agent_dispatch.list_dispatch(room)
        except api.TwirpError:
            sent = []  # the room does not exist yet
        # A room made by someone just joining has a nameless dispatch; ignore it.
        if not any(d.agent_name == "lecturer" for d in sent):
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name="lecturer", room=room, metadata=json.dumps(meta)
                )
            )


class Student:
    def __init__(self, name: str, role: str = "student") -> None:
        self.name = name
        self.role = role
        self.floor = FloorState()
        self.seen: list[dict[str, Any]] = []  # every event applied, in order
        self.heard_people: dict[str, float] = {}  # identity -> ms of sound from that person
        self.reactions: list[tuple[str, str]] = []  # who, kind: reactions seen go by
        self._gap_check = False
        self.room = rtc.Room()
        self.board = Board()
        self.stage = Stage()
        self.last_seq = 0
        self.synced = False
        self.phase = "unknown"
        self.buffer: dict[int, dict[str, Any]] = {}
        self.snapshot: dict[str, Any] | None = None
        self.snapshots = 0
        self.gaps = 0
        self.says: list[dict[str, Any]] = []  # each sentence, with what came with it
        self.board_at: dict[int, dict[str, Any]] = {}  # seq -> board after that event
        self.stage_at: dict[int, dict[str, Any]] = {}  # seq -> stage after that event
        self.shows: list[str] = []  # what the stage was changed to, in order
        self.video_frames = 0
        self.pauses: list[float] = []
        self.voiced_ms = 0.0
        self.onsets: list[float] = []  # times the voice started after a silence
        self._silent_ms = 1000.0
        self.ended = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def join(self, room_name: str) -> None:
        self.room.register_text_stream_handler(
            CLASS_TOPIC, lambda reader, who: self._spawn(self._on_text(reader, who))
        )
        self.room.on("track_subscribed", self._on_track)
        self.room.register_text_stream_handler(
            "ch.react", lambda reader, who: self._spawn(self._on_reaction(reader, who))
        )
        self.room.on("participant_connected", lambda p: self._spawn(self._sync()))
        await self.room.connect(URL, token(room_name, f"bot-{self.name}", self.name, self.role))
        self._spawn(self._sync())

    def _lecturer(self) -> rtc.RemoteParticipant | None:
        for p in self.room.remote_participants.values():
            if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                return p
        return None

    async def _sync(self) -> None:
        """Ask the lecturer for a snapshot. Retries: its RPC may not be up yet."""
        for _ in range(20):
            lecturer = self._lecturer()
            if lecturer and not self.synced:
                try:
                    await self.room.local_participant.perform_rpc(
                        destination_identity=lecturer.identity, method="ch.sync", payload=""
                    )
                    return
                except rtc.RpcError:
                    pass
            if self.synced:
                return
            await asyncio.sleep(0.25)

    async def _on_reaction(self, reader: rtc.TextStreamReader, who: str) -> None:
        self.reactions.append((who, (await reader.read_all()).strip()))

    async def _on_text(self, reader: rtc.TextStreamReader, who: str) -> None:
        lecturer = self._lecturer()
        if lecturer is None or who != lecturer.identity:
            return  # only the lecturer may speak on this topic
        msg = json.loads(await reader.read_all())
        if msg["type"] == "tick":
            return
        now = time.time()
        if msg["type"] == "snapshot":
            self.snapshots += 1
            self.snapshot = msg
            self.board = Board.from_snapshot(msg["board"])
            self.stage = Stage.from_snapshot(msg["stage"])
            self.floor = FloorState.from_snapshot(msg["floor"])
            self.last_seq = msg["seq"]
            self.phase = msg["phase"]
            self.synced = True
        else:
            for ev in msg["events"]:
                self.buffer[ev["seq"]] = {**ev, "_got": now, "_batch": msg["events"]}
        self._drain()

    def _drain(self) -> None:
        if not self.synced:
            return
        for seq in [s for s in self.buffer if s <= self.last_seq]:
            del self.buffer[seq]
        while self.last_seq + 1 in self.buffer:
            self._apply(self.buffer.pop(self.last_seq + 1))
        if self.buffer and not self._gap_check:
            # Something came early. Like a browser, give the missing event a moment
            # to arrive before calling it a gap and asking for a snapshot.
            self._gap_check = True
            self._spawn(self._check_gap())

    async def _check_gap(self) -> None:
        await asyncio.sleep(0.7)
        self._gap_check = False
        self._drain()
        if self.buffer and self.synced and min(self.buffer) > self.last_seq + 1:
            self.gaps += 1
            self.synced = False
            self._spawn(self._sync())

    def _apply(self, ev: dict[str, Any]) -> None:
        self.last_seq = ev["seq"]
        t = ev["type"]
        self.seen.append(
            {k: v for k, v in ev.items() if not k.startswith("_")} | {"got": ev["_got"]}
        )
        if t.startswith("board."):
            self.board.apply(ev)
        elif t.startswith("floor."):
            self.floor.apply(ev)
        elif t.startswith(("stage.", "slides.", "screen.", "video.")):
            self.stage.apply(ev)
            if t == "stage.show":
                self.shows.append(show_text(ev["rows"]))
        elif t == "say":
            board = [e["type"] for e in ev["_batch"] if e["type"].startswith("board.")]
            self.says.append({"text": ev["text"], "got": ev["_got"], "board": board})
        elif t == "pause":
            self.pauses.append(ev["seconds"])
        elif t == "phase":
            self.phase = ev["phase"]
            if ev["phase"] == "ended":
                self.ended.set()
        self.board_at[ev["seq"]] = copy.deepcopy(self.board.snapshot())
        self.stage_at[ev["seq"]] = copy.deepcopy(self.stage.snapshot())

    def _on_track(self, track: rtc.Track, _pub: Any, person: rtc.RemoteParticipant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            if person.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                self._spawn(self._listen(track))
            else:
                self._spawn(self._listen_person(track, person.identity))
        elif track.kind == rtc.TrackKind.KIND_VIDEO:
            self._spawn(self._watch(track))

    async def _watch(self, track: rtc.Track) -> None:
        async for _ in rtc.VideoStream(track):
            self.video_frames += 1

    async def _listen_person(self, track: rtc.Track, identity: str) -> None:
        """Add up how long we hear a person (a student or staff), not the lecturer."""
        async for ev in rtc.AudioStream(track, sample_rate=16000, num_channels=1):
            pcm = np.frombuffer(ev.frame.data, dtype=np.int16).astype(np.float32) / 32768
            if float(np.sqrt(np.mean(pcm * pcm))) > VOICED:
                heard = self.heard_people.get(identity, 0.0)
                self.heard_people[identity] = heard + ev.frame.duration * 1000

    async def _listen(self, track: rtc.Track) -> None:
        async for ev in rtc.AudioStream(track, sample_rate=16000, num_channels=1):
            pcm = np.frombuffer(ev.frame.data, dtype=np.int16).astype(np.float32) / 32768
            ms = ev.frame.duration * 1000
            if float(np.sqrt(np.mean(pcm * pcm))) > VOICED:
                if self._silent_ms >= 100:
                    self.onsets.append(time.time())
                self.voiced_ms += ms
                self._silent_ms = 0.0
            else:
                self._silent_ms += ms


def show_text(rows: list[list[dict[str, Any]]]) -> str:
    """Rows of panes as the script writes them: `slides 2 | board / screen`."""

    def one(pane: dict[str, Any]) -> str:
        fixed = pane.get("slide") or (f"page {pane['page']}" if pane.get("page") else "")
        return f"{pane['source']} {fixed}".strip()

    return " / ".join(" | ".join(one(pane) for pane in row) for row in rows)


def sorted_shows(student: Student) -> list[str]:
    """The stage changes as the log's sequence numbers order them."""
    order = sorted(student.stage_at)
    out: list[str] = []
    last = None
    for seq in order:
        panes = show_text(student.stage_at[seq]["rows"])
        if panes != last and (last is not None or panes != "board"):
            out.append(panes)
        last = panes
    return out


def newest_log(room: str) -> list[dict[str, Any]]:
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "logs")
    files = sorted(glob.glob(os.path.join(root, f"{room}-*.jsonl")))
    if not files:
        return []
    with open(files[-1]) as f:
        return [json.loads(line) for line in f]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("room")
    ap.add_argument("--lesson", required=True, help="<course>/<lesson>")
    ap.add_argument("--pause-scale", type=float, default=1.0)
    ap.add_argument("--start-delay", type=float, default=2.0)
    ap.add_argument("--seconds", type=float, default=0, help="0 = stay until the lesson ends")
    ap.add_argument("--late-join", type=float, default=0, help="second student joins after N s")
    ap.add_argument("--chat", default="", help="a chat message to send")
    ap.add_argument("--chat-at", type=float, default=10, help="when to send it, in seconds")
    args = ap.parse_args()
    meta = {
        "lesson": args.lesson,
        "pause_scale": args.pause_scale,
        "start_delay": args.start_delay,
    }

    await ensure_lecturer(args.room, meta)
    first = Student("Ada")
    await first.join(args.room)
    late: Student | None = None

    async def late_joiner() -> None:
        nonlocal late
        await asyncio.sleep(args.late_join)
        late = Student("Late")
        await late.join(args.room)

    async def chatter() -> None:
        await asyncio.sleep(args.chat_at)
        await first.room.local_participant.send_text(args.chat, topic="ch.chat")

    if args.late_join:
        late_task = asyncio.create_task(late_joiner())
    if args.chat:
        chat_task = asyncio.create_task(chatter())  # noqa: F841, RUF006
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(first.ended.wait(), timeout=args.seconds or 900)
    await asyncio.sleep(0.5)
    if args.late_join:
        late_task.cancel()

    # How far each caption was from the voice starting.
    offsets = []
    for say in first.says:
        near = [o - say["got"] for o in first.onsets if abs(o - say["got"]) < 0.6]
        if near:
            offsets.append(min(near, key=abs) * 1000)
    with_board = sum(1 for s in first.says if s["board"])

    log = newest_log(args.room)
    tts = [e for e in log if e["type"] == "note.tts" and e.get("first_audio_ms") is not None]
    first_audio = sorted(e["first_audio_ms"] for e in tts)
    heard = sum(1 for e in log if e["type"] == "heard")

    checks = {
        "got_a_snapshot": first.snapshots >= 1,
        "heard_the_voice": first.voiced_ms > 1000,
        "saw_board_events": len(first.board_at) > 0 and with_board > 0,
        "no_gaps_in_sequence": first.gaps == 0,
    }
    if first.shows:
        checks["stage_changes_in_order"] = first.shows == sorted_shows(first)
    if any("screen" in show for show in first.shows):
        checks["received_screen_video"] = first.video_frames > 10
    summary: dict[str, Any] = {
        "room": args.room,
        "phase_at_end": first.phase,
        "events_applied": first.last_seq,
        "sentences": len(first.says),
        "sentences_with_board_changes": with_board,
        "voice_seconds": round(first.voiced_ms / 1000, 1),
        "voice_starts_heard": len(first.onsets),
        "pauses": first.pauses,
        "final_board": [ln["content"] for ln in first.board.snapshot()["lines"]],
    }
    if first.shows:
        summary["stage_changes"] = first.shows
        summary["screen_video_frames"] = first.video_frames
        summary["screen_tabs"] = [tab["title"] for tab in first.stage.screen["tabs"]]
        summary["screen_active_tab"] = first.stage.screen["active"] + 1
        failed = [e for e in log if e["type"] in ("note.screen_failed", "note.step_failed")]
        summary["steps_that_failed"] = [e.get("error") for e in failed]
    if offsets:
        summary["voice_start_minus_caption_ms"] = {
            "median": round(statistics.median(offsets)),
            "worst": round(max(offsets, key=abs)),
            "matched": len(offsets),
        }
    if first_audio:
        summary["text_to_first_audio_ms"] = {
            "median": round(statistics.median(first_audio)),
            "p90": first_audio[int(len(first_audio) * 0.9) - 1],
            "worst": first_audio[-1],
            "sentences": len(first_audio),
        }
        summary["sentences_logged_as_heard"] = heard
        late_audio = sum(1 for e in tts if not e["ready_before_needed"])
        summary["sentences_whose_audio_was_not_ready_in_time"] = late_audio
    if args.late_join:
        if late is None or late.snapshot is None:
            checks["late_joiner_snapshot_matches"] = False
        else:
            seq = late.snapshot["seq"]
            expected = first.board_at.get(seq)
            checks["late_joiner_snapshot_matches"] = (
                expected is not None
                and expected == late.snapshot["board"]
                and first.stage_at.get(seq) == late.snapshot["stage"]
            )
            checks["late_joiner_ends_with_same_board"] = (
                late.board.snapshot() == first.board.snapshot()
                and late.stage.snapshot() == first.stage.snapshot()
                and late.gaps == 0
            )
            summary["late_joiner"] = {
                "joined_at_seq": seq,
                "lines_on_board_then": len(late.snapshot["board"]["lines"]),
                "caption_then": late.snapshot["caption"],
            }
    summary["checks"] = checks
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    await first.room.disconnect()
    if late:
        await late.room.disconnect()
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
