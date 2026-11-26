"""Which model does which job.

Each job has a setting that names its model, as `provider:model`. Left out or `off`,
the job is not done, and the class goes on without it.

    CH_WRITER      writes the class as it is taught (a live class). Without it, every
                   class is read from its script.
    CH_PLANNER     reshapes the rest of a live class at checkpoints.
    CH_DECISIONS   sorts chat messages for the live teacher, quickly: is this for the
                   lecturer, is it urgent, is it an answer, is the answer right. Made for
                   decision models, which answer with a probability; any model that can
                   reply in JSON will do. Without it, the teacher is shown every message
                   and judges for itself.
    CH_QUESTIONS   while a script is read: says what kind of thing a chat message is,
                   and answers quick questions.
    CH_ANSWERS     while a script is read: answers, at the end, the questions that were
                   put aside.
    CH_HEARING     writes down what people say aloud (`speech/hearing.py`). A model that
                   takes audio in, such as one on OpenRouter.
    CH_IMAGES      makes a picture during a class when the lecturer asks for one
                   (`generate` in a script). An image model, not a chat model: see
                   `models/images.py`. CH_IMAGES_STYLE is put before every description.

Providers:

    anthropic      Claude. Needs ANTHROPIC_API_KEY.
    openai         Needs OPENAI_API_KEY.
    openrouter     Most models, through one key. Needs OPENROUTER_API_KEY.
    <any name>     A server of your own that speaks the OpenAI chat completions API
                   (Ollama, LM Studio, vLLM ...). Give it a name and set
                   CH_PROVIDER_<NAME>_URL, and CH_PROVIDER_<NAME>_KEY if it wants a key.

    CH_WRITER=anthropic:<model>
    CH_DECISIONS=openrouter:<decision model>
    CH_PROVIDER_LOCAL_URL=http://localhost:11434/v1
    CH_QUESTIONS=local:<model>

CH_<JOB>_EXTRA is JSON laid over every request for that job, for anything a service
has that is not general: CH_DECISIONS_EXTRA={"reasoning": {"enabled": false}}. A null
takes a parameter out.

Settings are read when they are needed, not when the code loads, so they can come from
`agent/.env.local`. To see what is set, and try each model with one small request:

    cd agent && uv run python -m lecturer.settings          what each job uses
    cd agent && uv run python -m lecturer.settings --try    and one request to each
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .models.images import DEFAULT_STYLE, ImageModel
from .models.providers import AnthropicModel, CompatibleModel, Model

logger = logging.getLogger("lecturer.settings")

JOBS = ("writer", "planner", "decisions", "questions", "answers", "hearing")
IMAGES = "images"  # set in the same way, but it is an image model, not a chat model
OFF = ("", "off", "none", "0", "false", "no")

# Services with a fixed address: where it is, and the setting that holds the key.
KNOWN = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
}


class SettingsError(ValueError):
    """A setting is written wrongly. Said loudly, not passed over."""


def _extra(job: str) -> dict[str, Any]:
    name = f"CH_{job.upper()}_EXTRA"
    text = os.environ.get(name, "").strip()
    if not text:
        return {}
    try:
        extra = json.loads(text)
    except ValueError:
        raise SettingsError(f"{name} is not JSON") from None
    if not isinstance(extra, dict):
        raise SettingsError(f"{name} should be a JSON object")
    return extra


def describe(job: str) -> tuple[str, str]:
    """A job's setting, and why it is off ("" if it is on)."""
    name = f"CH_{job.upper()}"
    spec = os.environ.get(name, "").strip()
    if spec.lower() in OFF:
        return spec, f"{name} is not set"
    provider, _, model = spec.partition(":")
    provider = provider.lower()
    if not provider or not model:
        raise SettingsError(f"{name} should be provider:model, not {spec!r}")
    if provider == "anthropic":
        if job == IMAGES:
            return spec, "anthropic has no image model: use another provider for pictures"
        if job == "hearing":
            return spec, "hearing is sent as OpenAI-style audio: use another provider for it"
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            return spec, "ANTHROPIC_API_KEY is not set"
    elif provider in KNOWN:
        if not os.environ.get(KNOWN[provider][1]):
            return spec, f"{KNOWN[provider][1]} is not set"
    elif not os.environ.get(f"CH_PROVIDER_{provider.upper()}_URL"):
        return (
            spec,
            f"there is no provider called {provider}: CH_PROVIDER_{provider.upper()}_URL is not set",
        )
    return spec, ""


_told: set[str] = set()


def _on(job: str) -> tuple[str, str, str] | None:
    """A job's setting, provider and model, or None (said once) if the job is off."""
    spec, off = describe(job)
    if off:
        # A model that is named but cannot be reached is worth saying, once.
        if spec.lower() not in OFF and job not in _told:
            _told.add(job)
            logger.warning("%s is off: %s", job, off)
        return None
    provider, _, model = spec.partition(":")
    return spec, provider.lower(), model


def _address(provider: str) -> tuple[str, str]:
    """Where a provider that speaks the OpenAI API is, and its key."""
    if provider in KNOWN:
        url, key = KNOWN[provider]
        return url, os.environ[key]
    name = provider.upper()
    return os.environ[f"CH_PROVIDER_{name}_URL"], os.environ.get(f"CH_PROVIDER_{name}_KEY", "")


def limit(job: str, default: int | None = None) -> int | None:
    """The most calls one class may make to a job's model. None is no limit.

    Nothing is limited unless whoever runs this sets CH_<JOB>_LIMIT.
    """
    name = f"CH_{job.upper()}_LIMIT"
    text = os.environ.get(name, "").strip()
    if not text:
        return default
    try:
        return max(0, int(text))
    except ValueError:
        raise SettingsError(f"{name} must be a whole number, not {text!r}") from None


def image_model() -> ImageModel | None:
    """The image model, or None if pictures are not made."""
    on = _on(IMAGES)
    if on is None:
        return None
    spec, provider, model = on
    url, key = _address(provider)
    style = os.environ.get("CH_IMAGES_STYLE", DEFAULT_STYLE).strip()
    return ImageModel(
        spec, model, base_url=url, api_key=key, provider=provider, style=style, extra=_extra(IMAGES)
    )


def model_for(job: str) -> Model | None:
    """The model set for a job, or None if the job is off."""
    on = _on(job)
    if on is None:
        return None
    spec, provider, model = on
    extra = _extra(job)
    if provider == "anthropic":
        return AnthropicModel(spec, model, extra=extra)
    url, key = _address(provider)
    return CompatibleModel(spec, model, base_url=url, api_key=key, provider=provider, extra=extra)


async def _try(model: Model) -> str:
    import asyncio
    import time

    from .models.providers import ModelError

    began = time.monotonic()
    try:
        reply = await asyncio.wait_for(
            model.complete(
                "", [{"role": "user", "content": "Reply with the one word: ready"}], max_tokens=200
            ),
            30,
        )
    except TimeoutError:
        return "no reply in 30 s"
    except ModelError as e:
        return f"failed: {e}"
    return f"replied {reply.text.strip()[:40]!r} in {time.monotonic() - began:.1f} s"


def main() -> None:
    import asyncio
    import sys
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env.local")
    for job in (*JOBS, IMAGES):
        spec, off = describe(job)
        line = f"{job:<10} {spec or 'off'}"
        if off and spec.lower() not in OFF:
            line += f"   (off: {off})"
        elif not off and job == IMAGES:
            line += "   (not tried: a picture costs more than a word)"
        elif not off and "--try" in sys.argv:
            model = model_for(job)
            assert model is not None
            line += "   " + asyncio.run(_try(model))
        print(line)


if __name__ == "__main__":
    main()
