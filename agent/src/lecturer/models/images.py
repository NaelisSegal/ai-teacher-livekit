"""The image model: pictures made during a class, from a description.

Which model is a setting, `CH_IMAGES` (see `settings.py`). Two ways of asking are
known, and the provider decides which is used:

    the images API      POST <address>/images/generations, as OpenAI has it. Used for
                        `openai` and for a server of your own.
    chat with pictures  POST <address>/chat/completions asking for an image back, as
                        OpenRouter has it for the image models it reaches.

Either way the answer is the picture's bytes. `CH_IMAGES_EXTRA` is JSON laid over the
request (a size, a quality), and `CH_IMAGES_STYLE` is put before every description, so
a class's pictures look alike.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

import aiohttp

from .providers import ModelError, merged

logger = logging.getLogger("lecturer.images")

DEFAULT_STYLE = "A clear, simple picture for teaching, on a plain light background."


def picture_type(data: bytes) -> str | None:
    """The file ending for these bytes, or None if they are not a picture we show."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


class ImageModel:
    def __init__(
        self,
        name: str,
        model: str,
        *,
        base_url: str,
        api_key: str,
        provider: str = "",
        style: str = DEFAULT_STYLE,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self._url = base_url.rstrip("/")
        self._key = api_key
        self._provider = provider
        self._style = style
        self._extra = extra or {}

    def _prompt(self, description: str) -> str:
        return f"{self._style}\n\n{description}".strip()

    async def make(self, description: str, *, timeout: float = 90.0) -> tuple[bytes, str]:
        """The picture and its file ending. Raises ModelError if there is none."""
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        limit = aiohttp.ClientTimeout(total=timeout)
        try:
            async with aiohttp.ClientSession(timeout=limit, headers=headers) as http:
                if self._provider == "openrouter":
                    data = await self._by_chat(http, description)
                else:
                    data = await self._by_images_api(http, description)
        except aiohttp.ClientResponseError as e:
            raise ModelError(f"{e.status} from the image model") from e
        except (aiohttp.ClientError, TimeoutError) as e:
            raise ModelError(f"could not reach the image model: {type(e).__name__}") from e
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise ModelError("the image model's answer had no picture in it") from e
        kind = picture_type(data)
        if kind is None:
            raise ModelError("the image model's answer was not a picture")
        return data, kind

    @staticmethod
    async def _post(http: aiohttp.ClientSession, url: str, body: dict[str, Any]) -> Any:
        async with http.post(url, json=body) as response:
            response.raise_for_status()
            return await response.json()

    @staticmethod
    async def _fetch(http: aiohttp.ClientSession, url: str) -> bytes:
        # The picture is on the service's own storage, which must not be sent our key.
        async with aiohttp.ClientSession(timeout=http.timeout) as plain, plain.get(url) as got:
            got.raise_for_status()
            return await got.read()

    async def _by_images_api(self, http: aiohttp.ClientSession, description: str) -> bytes:
        body = merged(
            {"model": self.model, "prompt": self._prompt(description), "n": 1}, self._extra
        )
        made = (await self._post(http, f"{self._url}/images/generations", body))["data"][0]
        if made.get("b64_json"):
            return base64.b64decode(made["b64_json"])
        return await self._fetch(http, made["url"])

    async def _by_chat(self, http: aiohttp.ClientSession, description: str) -> bytes:
        body = merged(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": self._prompt(description)}],
                "modalities": ["image", "text"],
            },
            self._extra,
        )
        answer = await self._post(http, f"{self._url}/chat/completions", body)
        address = answer["choices"][0]["message"]["images"][0]["image_url"]["url"]
        if not address.startswith("data:"):
            return await self._fetch(http, address)
        return base64.b64decode(address.split(",", 1)[1])
