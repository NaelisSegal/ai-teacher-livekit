"""The models, whoever makes them.

Every model the lecturer uses is reached through one small interface, `Model`:

    complete   one request, one reply. With a `schema`, the reply is JSON of that shape.
    stream     a reply given piece by piece, as it is written.

Two kinds of service are behind it:

    AnthropicModel     Claude, through Anthropic's own API.
    CompatibleModel    anything that speaks the OpenAI chat completions API: OpenAI,
                       OpenRouter (which reaches most models), and servers of your own
                       such as Ollama, LM Studio and vLLM.

Which model does which job is a setting (`settings.py`). Nothing here names one.

A caller says what it wants in general terms: `think=False` (answer at once), an
`effort`, a `schema`. Each kind of service turns that into its own parameters where it
has them. Anything more particular goes in the job's EXTRA setting, which is merged
into every request as it is, so any parameter a service has can be reached.

Whatever goes wrong (an error, a refusal, a reply cut short) is a `ModelError`. The
caller decides what a class does without the answer.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

Messages = list[dict[str, Any]]


class ModelError(Exception):
    """The model gave nothing usable: an error, a refusal, or it was too slow."""


@dataclass
class Usage:
    """What one request used, in tokens."""

    tokens_in: int = 0  # not from the cache
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def any(self) -> bool:
        return bool(self.tokens_in or self.tokens_out or self.cache_read or self.cache_write)


@dataclass
class Reply:
    text: str
    usage: Usage = field(default_factory=Usage)


class Model(Protocol):
    name: str  # as in the setting, `provider:model`

    async def complete(
        self,
        system: str,
        messages: Messages,
        *,
        max_tokens: int,
        schema: dict[str, Any] | None = None,
        think: bool | None = None,
        effort: str | None = None,
        cache: bool = False,
        retries: int = 0,
    ) -> Reply:
        """One reply. `cache`: the system text is the same on every call."""
        ...

    def stream(
        self,
        system: str,
        messages: Messages,
        usage: Usage,
        *,
        max_tokens: int,
        think: bool | None = None,
        effort: str | None = None,
        cache: bool = False,
        growing: bool = False,
    ) -> AsyncIterator[str]:
        """The reply's text in pieces. `usage` is filled in as it goes, so it is right
        even if the reader stops early. `growing`: the conversation only ever grows."""
        ...


def merged(request: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """`request` with `extra` laid over it. A null in `extra` takes that key out."""
    out = dict(request)
    for key, value in extra.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merged(out[key], value)
        else:
            out[key] = value
    return out


def parse_json(text: str) -> Any:
    """A reply that should be JSON, as data. Raises ValueError if it is not.

    A model without strict structured output may wrap its JSON in a code fence or a
    sentence, so the outermost {...} is tried as well.
    """
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise
        return json.loads(text[start : end + 1])


# -- Claude, through Anthropic's API ------------------------------------------------------


def _no_thinking(model: str) -> dict[str, Any] | None:
    """How this Claude model is told to answer without thinking first, if it can be."""
    if model.startswith("claude-sonnet-5-5"):
        return {"type": "between_tools"}
    if model.startswith(("claude-opus-5-5", "claude-fable", "claude-mythos")):
        return None  # thinking cannot be turned off; a low effort is the control
    return {"type": "disabled"}


class AnthropicModel:
    def __init__(self, name: str, model: str, *, extra: dict[str, Any] | None = None) -> None:
        import anthropic

        self.name = name
        self.model = model
        self._extra = extra or {}
        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(max_retries=0)

    def request(
        self,
        system: str,
        messages: Messages,
        *,
        max_tokens: int,
        schema: dict[str, Any] | None = None,
        think: bool | None = None,
        effort: str | None = None,
        cache: bool = False,
        growing: bool = False,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system and cache:
            request["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        elif system:
            request["system"] = system
        if growing:
            request["cache_control"] = {"type": "ephemeral"}
        output: dict[str, Any] = {}
        if effort:
            output["effort"] = effort
        if schema is not None:
            output["format"] = {"type": "json_schema", "schema": schema}
        if output:
            request["output_config"] = output
        if think is False and (off := _no_thinking(self.model)) is not None:
            request["thinking"] = off
        return merged(request, self._extra)

    def _explain(self, error: Exception) -> str:
        if isinstance(error, self._anthropic.RateLimitError):
            return "rate limited"
        if isinstance(error, self._anthropic.APIStatusError):
            return f"{error.status_code} {error.message}"
        if isinstance(error, self._anthropic.APIConnectionError):
            return "could not reach the model"
        return type(error).__name__

    @staticmethod
    def _count(usage: Usage, counted: Any) -> None:
        usage.tokens_in = counted.input_tokens or 0
        usage.cache_read = getattr(counted, "cache_read_input_tokens", 0) or 0
        usage.cache_write = getattr(counted, "cache_creation_input_tokens", 0) or 0

    async def complete(
        self,
        system: str,
        messages: Messages,
        *,
        max_tokens: int,
        schema: dict[str, Any] | None = None,
        think: bool | None = None,
        effort: str | None = None,
        cache: bool = False,
        retries: int = 0,
    ) -> Reply:
        request = self.request(
            system, messages, max_tokens=max_tokens, schema=schema, think=think, effort=effort,
            cache=cache,
        )  # fmt: skip
        try:
            client = self._client.with_options(max_retries=retries)
            response = await client.messages.create(**request)
        except Exception as e:  # whatever went wrong, the caller hears one thing
            raise ModelError(self._explain(e)) from e
        usage = Usage(tokens_out=response.usage.output_tokens or 0)
        self._count(usage, response.usage)
        if response.stop_reason != "end_turn":
            raise ModelError(f"it stopped with {response.stop_reason}")  # refused, or cut short
        return Reply("".join(b.text for b in response.content if b.type == "text"), usage)

    async def stream(
        self,
        system: str,
        messages: Messages,
        usage: Usage,
        *,
        max_tokens: int,
        think: bool | None = None,
        effort: str | None = None,
        cache: bool = False,
        growing: bool = False,
    ) -> AsyncIterator[str]:
        request = self.request(
            system, messages, max_tokens=max_tokens, think=think, effort=effort, cache=cache,
            growing=growing,
        )  # fmt: skip
        stop = ""
        try:
            async with self._client.messages.stream(**request) as stream:
                async for event in stream:
                    if event.type == "message_start":
                        self._count(usage, event.message.usage)
                    elif event.type == "message_delta":
                        if event.usage and event.usage.output_tokens is not None:
                            usage.tokens_out = event.usage.output_tokens
                        stop = event.delta.stop_reason or stop
                    elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                        # A reply stopped early has written a little more than the last
                        # count says; that is not counted.
                        usage.tokens_out = max(usage.tokens_out, 1)
                        yield event.delta.text
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except Exception as e:  # a dropped connection comes through as it is
            raise ModelError(self._explain(e)) from e
        if stop == "refusal":
            raise ModelError("the request was refused")


# -- anything that speaks the OpenAI chat completions API ---------------------------------


class CompatibleModel:
    """A model behind an OpenAI-compatible chat completions API.

    `think` and `effort` are not sent: services disagree about how they are spelled
    and many models have neither. Put the service's own parameter in the job's EXTRA
    setting, such as {"reasoning": {"enabled": false}} on OpenRouter.
    """

    def __init__(
        self,
        name: str,
        model: str,
        *,
        base_url: str,
        api_key: str,
        provider: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        import openai

        self.name = name
        self.model = model
        self._provider = provider
        self._extra = extra or {}
        self._openai = openai
        # A server of your own may want no key, but the client insists on some text.
        self._client = openai.AsyncOpenAI(
            base_url=base_url, api_key=api_key or "none", max_retries=0
        )

    def request(
        self,
        system: str,
        messages: Messages,
        *,
        max_tokens: int,
        schema: dict[str, Any] | None = None,
        stream: bool = False,
    ) -> dict[str, Any]:
        said = [{"role": "system", "content": system}] if system else []
        request: dict[str, Any] = {"model": self.model, "messages": said + messages}
        # OpenAI's newer models take only the newer name for the limit.
        request["max_completion_tokens" if self._provider == "openai" else "max_tokens"] = (
            max_tokens
        )
        if schema is not None:
            request["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "reply", "strict": True, "schema": schema},
            }
            if self._provider == "openrouter":
                # Only send it to a host that will keep to the schema.
                request["provider"] = {"require_parameters": True}
        if stream:
            request["stream"] = True
            request["stream_options"] = {"include_usage": True}
        return merged(request, self._extra)

    async def _send(self, request: dict[str, Any], retries: int = 0) -> Any:
        known = {k: request.pop(k) for k in ("model", "messages", "stream") if k in request}
        client = self._client.with_options(max_retries=retries)
        return await client.chat.completions.create(**known, extra_body=request)

    def _explain(self, error: Exception) -> str:
        if isinstance(error, self._openai.RateLimitError):
            return "rate limited"
        if isinstance(error, self._openai.APIStatusError):
            return f"{error.status_code} {error.message}"
        if isinstance(error, self._openai.APIConnectionError):
            return "could not reach the model"
        return type(error).__name__

    @staticmethod
    def _count(usage: Usage, counted: Any) -> None:
        if counted is None:
            return
        details = getattr(counted, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0
        usage.cache_read = cached
        usage.tokens_in = max((getattr(counted, "prompt_tokens", 0) or 0) - cached, 0)
        usage.tokens_out = getattr(counted, "completion_tokens", 0) or 0

    @staticmethod
    def _check(finish: str | None) -> None:
        if finish == "length":
            raise ModelError("the reply was cut short")
        if finish == "content_filter":
            raise ModelError("the request was refused")

    async def complete(
        self,
        system: str,
        messages: Messages,
        *,
        max_tokens: int,
        schema: dict[str, Any] | None = None,
        think: bool | None = None,
        effort: str | None = None,
        cache: bool = False,
        retries: int = 0,
    ) -> Reply:
        request = self.request(system, messages, max_tokens=max_tokens, schema=schema)
        try:
            response = await self._send(request, retries)
        except Exception as e:
            raise ModelError(self._explain(e)) from e
        usage = Usage()
        self._count(usage, getattr(response, "usage", None))
        if not getattr(response, "choices", None):
            raise ModelError("the reply was empty")
        choice = response.choices[0]
        self._check(choice.finish_reason)
        return Reply(choice.message.content or "", usage)

    async def stream(
        self,
        system: str,
        messages: Messages,
        usage: Usage,
        *,
        max_tokens: int,
        think: bool | None = None,
        effort: str | None = None,
        cache: bool = False,
        growing: bool = False,
    ) -> AsyncIterator[str]:
        request = self.request(system, messages, max_tokens=max_tokens, stream=True)
        finish: str | None = None
        try:
            async with await self._send(request) as chunks:
                async for chunk in chunks:
                    self._count(usage, getattr(chunk, "usage", None))
                    if not chunk.choices:
                        continue
                    finish = chunk.choices[0].finish_reason or finish
                    text = chunk.choices[0].delta.content
                    if text:
                        # Not every service counts as it goes, or at all.
                        usage.tokens_out = max(usage.tokens_out, 1)
                        yield text
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except Exception as e:
            raise ModelError(self._explain(e)) from e
        self._check(finish)
