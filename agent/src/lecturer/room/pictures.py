"""Pictures made during a class.

When the lecturer asks for a picture (`generate`), its place on the board or the slide
is kept at once, and this makes the picture beside the lesson, which goes on. When it
is ready it is saved where the web app can serve it, and a `fill` event puts it in its
place. If it cannot be made, the place is closed up and the lecturer is shown that.

Made pictures are kept in one folder per class (`generated/<room>/`, or under
CH_GENERATED_DIR). The web app serves them from there, so for now the lecturer and
the web app must be able to reach the same folder, as they must for `lessons/`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import uuid
from pathlib import Path

from ..models.images import ImageModel
from ..models.providers import ModelError
from .events import ClassEvents

logger = logging.getLogger("lecturer.pictures")

DEFAULT_DIR = Path(__file__).resolve().parents[4] / "generated"


def generated_dir() -> Path:
    return Path(os.environ.get("CH_GENERATED_DIR") or DEFAULT_DIR).resolve()


class Pictures:
    def __init__(
        self, events: ClassEvents, model: ImageModel, room: str, *, most: int | None = None
    ) -> None:
        self._events = events
        self._model = model
        self._room = re.sub(r"[^\w-]", "_", room)[:64] or "class"  # it names a folder
        self._left = most  # pictures this class may still make. None is no limit
        self._count = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self.made = 0

    def make(self, line_id: str, description: str, on: str) -> None:
        """Start making the picture for this line. `on` is "board" or "slide"."""
        task = asyncio.create_task(self._make(line_id, description, on))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _waiting(self, line_id: str, on: str) -> bool:
        if on == "slide":
            inked = (ln for lines in self._events.stage.ink.values() for ln in lines)
            return any(ln["id"] == line_id and ln.get("pending") for ln in inked)
        lines = (ln for page in self._events.board.pages.values() for ln in page)
        return any(ln.id == line_id and ln.pending for ln in lines)

    async def _make(self, line_id: str, description: str, on: str) -> None:
        fill = {"type": "slides.fill" if on == "slide" else "board.fill", "id": line_id}
        began = time.monotonic()
        content = ""
        try:
            if self._left is not None:
                if self._left <= 0:
                    raise ModelError("this class has had all the pictures it may make")
                self._left -= 1
            data, ending = await self._model.make(description)
            self._count += 1
            name = f"{self._count}-{uuid.uuid4().hex[:8]}.{ending}"
            folder = generated_dir() / self._room
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(data)
            content = f"class-files/{self._room}/{name}"
            self.made += 1
        except ModelError as e:
            logger.warning("a picture could not be made: %s", e)
        except OSError as e:
            logger.error("a picture could not be saved: %s", e)
        except Exception:  # an answer of a shape nobody foresaw: its place is still closed
            logger.exception("a picture could not be made")
        self._events.note("picture", made=bool(content), seconds=round(time.monotonic() - began, 1))
        # Its place is written with the sentence that asked for it, which may be a
        # moment after this was started.
        for _ in range(100):
            if self._waiting(line_id, on):
                break
            await asyncio.sleep(0.05)
        await self._events.emit({**fill, "content": content, "failed": not content})

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
