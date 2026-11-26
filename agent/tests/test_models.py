"""The models behind one interface: settings, both kinds of service, and the jobs.

No real service is called. The OpenAI-compatible kind talks to a pretend server on
this machine; the Claude kind is checked by the requests it would send.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from lecturer import settings
from lecturer.models.chat_answers import ChatAnswerer
from lecturer.models.decisions import SCHEMA, ModelDecisions
from lecturer.models.providers import (
    AnthropicModel,
    CompatibleModel,
    ModelError,
    Usage,
    merged,
    parse_json,
)
from lecturer.models.writer_planner import Planner, Writer

KEYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "OPENROUTER_API_KEY")


@pytest.fixture(autouse=True)
def no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    for job in settings.JOBS:
        monkeypatch.delenv(f"CH_{job.upper()}", raising=False)
        monkeypatch.delenv(f"CH_{job.upper()}_EXTRA", raising=False)
    settings._told.clear()


# -- settings ---------------------------------------------------------------------------


def test_a_job_with_no_setting_is_off() -> None:
    assert settings.model_for("writer") is None


def test_each_provider_is_reached_its_own_way(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("CH_PROVIDER_LOCAL_URL", "http://localhost:1/v1")
    monkeypatch.setenv("CH_WRITER", "anthropic:some-claude")
    monkeypatch.setenv("CH_DECISIONS", "openrouter:maker/model:free")
    monkeypatch.setenv("CH_QUESTIONS", "local:a-model")
    monkeypatch.setenv("CH_PLANNER", "off")

    writer = settings.model_for("writer")
    assert isinstance(writer, AnthropicModel) and writer.model == "some-claude"
    decisions = settings.model_for("decisions")
    # Only the first colon divides the provider from the model.
    assert isinstance(decisions, CompatibleModel) and decisions.model == "maker/model:free"
    assert isinstance(settings.model_for("questions"), CompatibleModel)
    assert settings.model_for("planner") is None


def test_a_model_that_cannot_be_reached_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CH_WRITER", "anthropic:some-claude")  # no key
    monkeypatch.setenv("CH_PLANNER", "nowhere:a-model")  # no such provider
    assert settings.model_for("writer") is None
    assert settings.model_for("planner") is None
    assert "ANTHROPIC_API_KEY" in settings.describe("writer")[1]
    assert "CH_PROVIDER_NOWHERE_URL" in settings.describe("planner")[1]


def test_a_setting_written_wrongly_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CH_WRITER", "just-a-model")
    with pytest.raises(settings.SettingsError):
        settings.model_for("writer")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("CH_WRITER", "anthropic:some-claude")
    monkeypatch.setenv("CH_WRITER_EXTRA", "{not json")
    with pytest.raises(settings.SettingsError):
        settings.model_for("writer")


def test_extra_is_laid_over_a_request_and_null_takes_a_key_out() -> None:
    request = {"a": 1, "nested": {"keep": 1, "drop": 2}, "gone": 3}
    extra = {"nested": {"drop": None, "new": 4}, "gone": None, "b": 2}
    assert merged(request, extra) == {"a": 1, "nested": {"keep": 1, "new": 4}, "b": 2}


def test_json_wrapped_in_other_text_is_still_read() -> None:
    assert parse_json('```json\n{"probability": 0.5}\n```') == {"probability": 0.5}
    with pytest.raises(ValueError):
        parse_json("no json here")


# -- Claude: the requests it would send ---------------------------------------------------


def test_the_writers_request_to_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    model = AnthropicModel("anthropic:claude-sonnet-5-5", "claude-sonnet-5-5")
    messages = [{"role": "user", "content": "hello"}]
    request = model.request(
        "the prep", messages, max_tokens=1500, think=False, effort="medium", cache=True,
        growing=True,
    )  # fmt: skip
    assert request == {
        "model": "claude-sonnet-5-5",
        "max_tokens": 1500,
        "system": [{"type": "text", "text": "the prep", "cache_control": {"type": "ephemeral"}}],
        "cache_control": {"type": "ephemeral"},
        "output_config": {"effort": "medium"},
        "thinking": {"type": "between_tools"},  # this model's "no thinking"
        "messages": messages,
    }


def test_no_thinking_is_said_the_way_each_claude_model_takes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    small = AnthropicModel("anthropic:claude-haiku-5-5", "claude-haiku-5-5")
    request = small.request("", [], max_tokens=100, schema=SCHEMA, think=False, effort="low")
    assert request["thinking"] == {"type": "disabled"}
    assert request["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    assert "system" not in request
    # A model that cannot stop thinking is not asked to.
    big = AnthropicModel("anthropic:claude-opus-5-5", "claude-opus-5-5")
    assert "thinking" not in big.request("", [], max_tokens=100, think=False)
    # And EXTRA can change or take out anything.
    extra = {"thinking": {"type": "adaptive"}, "output_config": {"effort": None}}
    own = AnthropicModel("anthropic:claude-haiku-5-5", "claude-haiku-5-5", extra=extra)
    request = own.request("", [], max_tokens=100, think=False, effort="low")
    assert request["thinking"] == {"type": "adaptive"}
    assert "output_config" not in request or "effort" not in request["output_config"]


# -- a pretend OpenAI-compatible server ---------------------------------------------------


class Pretend:
    """Answers chat completions with what it is told to, and keeps what it was sent."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.text = "ready"
        self.finish = "stop"
        self.status = 200
        self.usage: dict[str, Any] = {"prompt_tokens": 12, "completion_tokens": 3}

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.sent.append(body)
        if self.status != 200:
            return web.json_response({"error": {"message": "broken"}}, status=self.status)
        if not body.get("stream"):
            return web.json_response(
                {
                    "id": "x",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": self.text},
                            "finish_reason": self.finish,
                        }
                    ],
                    "usage": self.usage,
                }
            )
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)

        async def send(choices: list[dict[str, Any]], usage: Any = None) -> None:
            chunk = {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": body["model"],
                "choices": choices,
                "usage": usage,
            }
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())

        # In pieces that do not fall on line ends, as a real reply would not.
        for start in range(0, len(self.text), 7):
            piece = self.text[start : start + 7]
            await send([{"index": 0, "delta": {"content": piece}, "finish_reason": None}])
        await send([{"index": 0, "delta": {}, "finish_reason": self.finish}])
        await send([], self.usage)
        await response.write(b"data: [DONE]\n\n")
        return response


@pytest.fixture
async def pretend() -> AsyncIterator[tuple[Pretend, str]]:
    server = Pretend()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", server.handle)
    running = TestServer(app)
    await running.start_server()
    try:
        yield server, str(running.make_url("/v1"))
    finally:
        await running.close()


def compatible(url: str, provider: str = "local", **more: Any) -> CompatibleModel:
    return CompatibleModel(
        f"{provider}:a-model", "a-model", base_url=url, api_key="", provider=provider, **more
    )


async def test_a_reply_from_a_compatible_server(pretend: tuple[Pretend, str]) -> None:
    server, url = pretend
    server.usage = {
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "prompt_tokens_details": {"cached_tokens": 4},
    }
    model = compatible(url, extra={"reasoning": {"enabled": False}})
    reply = await model.complete(
        "be brief", [{"role": "user", "content": "hi"}], max_tokens=50, schema=SCHEMA
    )
    assert reply.text == "ready"
    assert (reply.usage.tokens_in, reply.usage.cache_read, reply.usage.tokens_out) == (8, 4, 3)
    sent = server.sent[0]
    assert sent["messages"][0] == {"role": "system", "content": "be brief"}
    assert sent["max_tokens"] == 50
    assert sent["response_format"]["json_schema"]["schema"] == SCHEMA
    assert sent["reasoning"] == {"enabled": False}  # from EXTRA
    assert "provider" not in sent and "stream" not in sent


async def test_what_differs_between_compatible_services(pretend: tuple[Pretend, str]) -> None:
    server, url = pretend
    await compatible(url, "openrouter").complete("", [], max_tokens=5, schema=SCHEMA)
    await compatible(url, "openai").complete("", [], max_tokens=5)
    assert server.sent[0]["provider"] == {"require_parameters": True}
    assert server.sent[1]["max_completion_tokens"] == 5 and "max_tokens" not in server.sent[1]


async def test_a_compatible_server_that_fails_or_cuts_short(pretend: tuple[Pretend, str]) -> None:
    server, url = pretend
    model = compatible(url)
    server.status = 500
    with pytest.raises(ModelError):
        await model.complete("", [], max_tokens=5)
    server.status, server.finish = 200, "length"
    with pytest.raises(ModelError):
        await model.complete("", [], max_tokens=5)
    with pytest.raises(ModelError):
        async for _ in model.stream("", [], Usage(), max_tokens=5):
            pass


# -- the jobs, on a compatible server -----------------------------------------------------


async def test_the_writer_gives_whole_lines_as_they_come(pretend: tuple[Pretend, str]) -> None:
    server, url = pretend
    server.text = "say: This is the first line.\n\nwrite a: x = 1\nsay: And the last"
    writer = Writer(compatible(url))
    lines = [line async for line in writer.lines("the prep", [{"role": "user", "content": "go"}])]
    assert lines == ["say: This is the first line.", "write a: x = 1", "say: And the last"]
    assert server.sent[0]["stream"] is True
    assert (writer.use.calls, writer.use.tokens_in, writer.use.tokens_out) == (1, 12, 3)
    assert len(writer.use.first_line_ms) == 1


async def test_a_writer_stopped_early_is_still_counted(pretend: tuple[Pretend, str]) -> None:
    server, url = pretend
    server.text = "say: One.\nsay: Two.\nsay: Three.\n"
    writer = Writer(compatible(url))
    lines = writer.lines("", [{"role": "user", "content": "go"}])
    assert await lines.__anext__() == "say: One."
    await lines.aclose()
    assert writer.use.calls == 1


async def test_a_writer_whose_first_line_is_late_has_failed() -> None:
    class Slow:
        name = "slow"

        async def stream(self, *_: Any, **__: Any) -> AsyncIterator[str]:
            await asyncio.sleep(5)
            yield "say: Too late.\n"

    writer = Writer(Slow(), first_line_timeout=0.05)  # type: ignore[arg-type]
    with pytest.raises(ModelError, match="too slow"):
        async for _ in writer.lines("", []):
            pass


async def test_the_planner_reads_a_plan(pretend: tuple[Pretend, str]) -> None:
    server, url = pretend
    plan = {
        "parts": [{"id": "2", "minutes": 3, "instruction": "shorter"}],
        "promises": [],
        "changed": True,
        "why": "time",
    }
    server.text = json.dumps(plan)
    planner = Planner(compatible(url))
    got = await planner.plan("the prep", "where the class is")
    assert got.changed and got.parts[0]["id"] == "2"
    server.text = "I would rather not say."
    with pytest.raises(ModelError):
        await planner.plan("the prep", "where the class is")


async def test_decisions_are_probabilities_or_nothing(pretend: tuple[Pretend, str]) -> None:
    server, url = pretend
    decisions = ModelDecisions(compatible(url))
    server.text = '{"probability": 0.8}'
    assert await decisions.ask_many(["a?", "b?"]) == [0.8, 0.8]
    server.text = '{"probability": 7}'
    assert await decisions.ask_many(["a?"]) == [None]
    server.status = 500
    assert await decisions.ask_many(["a?"]) == [None]
    assert decisions.calls == 3  # the failed request is not counted


async def test_chat_answers_with_and_without_a_stronger_model(pretend: tuple[Pretend, str]) -> None:
    server, url = pretend
    server.text = json.dumps(
        {"kind": "simple", "answer": [{"do": "say", "text": "You asked why. Because."}]}
    )
    answerer = ChatAnswerer(compatible(url), None)
    label = await answerer.label("why?", "the lesson so far")
    assert label is not None and label.kind == "simple"
    assert label.answer == "say: You asked why. Because."
    # With no model for put-aside questions there is no answer, and nothing is asked.
    assert await answerer.answer("a big question", "the lesson so far") is None
    assert len(server.sent) == 1


def test_what_a_lecturer_is_sent_reads_back_the_same(monkeypatch: Any) -> None:
    from lecturer.job import LecturerJob

    for name in ("CH_LESSON", "CH_LIVE", "CH_PAUSE_SCALE", "CH_START_DELAY"):
        monkeypatch.delenv(name, raising=False)
    group = {
        "id": "g1",
        "title": "Harder",
        "brief": "More problems",
        "main": "class-1",
        "minutes": 8,
        "said": ["Last sentence."],
        "private": False,
    }
    job = LecturerJob(lesson="course/lesson", live=True, group=group, start_delay=2)
    back = LecturerJob.from_json(job.to_json())
    assert back == LecturerJob(
        lesson="course/lesson", live=True, group=group, minutes=8, start_delay=2
    )
    assert LecturerJob.from_json('{"lesson": "c/l"}').live is False
    with pytest.raises(ValueError, match="no lesson"):
        LecturerJob.from_json("{}")
