"""What a lecturer is told when it is sent to a room: the LiveKit job's metadata.

Two things send for a lecturer, and both send this as JSON:

    the web app     when the first person joins a class (`ensureLecturer` in
                    web/app/api/connection-details/route.ts, type `LecturerJob`)
    a split class   for each group's room (`room/groups.py`)

Keep the three in step. The keys:

    lesson        <course>/<lesson>, the lesson to teach. Required (CH_LESSON stands in
                  for it, for trying a lecturer on its own).
    live          true: written as it is taught (needs a writer model, and a script with
                  parts); otherwise the script is read as written. (CH_LIVE)
    planner       false: no planner for this class, even if a planner model is set.
    minutes       the class's length, if not the script's.
    stay_seconds  keep the room open this long, for office hours with no script to end.
    group         set for the lecturer of a group that has split from a class:
                      id, title    the group's
                      brief        what it is for, if taught live
                      main         the main class's room, to go back to
                      minutes      how long the group has
                      said         the main class's last sentences, to carry on from
                      private      one student taken aside: nothing about it is reported

And two for development and the checks, which a real class leaves out:

    pause_scale   thinking pauses this many times as long (CH_PAUSE_SCALE)
    start_delay   seconds from the first person arriving to the start (CH_START_DELAY)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any


def is_on(value: Any) -> bool:
    """A setting from JSON or the environment: true, 1, yes."""
    return str(value).strip().lower() in ("1", "true", "yes")


@dataclass
class LecturerJob:
    lesson: str
    live: bool = False
    planner: bool = True
    minutes: float | None = None
    stay_seconds: float = 0.0
    group: dict[str, Any] | None = None
    pause_scale: float = 1.0
    start_delay: float = 4.0

    @classmethod
    def from_json(cls, text: str | None) -> LecturerJob:
        meta = json.loads(text or "{}")
        lesson = meta.get("lesson") or os.environ.get("CH_LESSON", "")
        if not lesson:
            raise ValueError("no lesson was named: set `lesson` in the job metadata, or CH_LESSON")
        group = meta.get("group") or None
        start_delay = float(meta.get("start_delay", os.environ.get("CH_START_DELAY", 4)))
        return cls(
            lesson=lesson,
            live=is_on(meta.get("live", os.environ.get("CH_LIVE", ""))),
            planner=is_on(meta.get("planner", 1)),
            minutes=float(meta.get("minutes") or (group or {}).get("minutes") or 0) or None,
            stay_seconds=float(meta.get("stay_seconds", 0)),
            group=group,
            pause_scale=float(meta.get("pause_scale", os.environ.get("CH_PAUSE_SCALE", 1))),
            # A group's students are already waiting: it starts sooner.
            start_delay=min(start_delay, 2) if group is not None else start_delay,
        )

    def to_json(self) -> str:
        """As sent: only what differs from the defaults, so it stays short."""
        defaults = LecturerJob(lesson=self.lesson)
        mine = asdict(self)
        # `live` always goes: left out, the lecturer would fall back on CH_LIVE.
        return json.dumps(
            {k: v for k, v in mine.items() if k in ("lesson", "live") or v != getattr(defaults, k)}
        )
