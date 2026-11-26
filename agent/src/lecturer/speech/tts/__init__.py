"""The voices.

Which one is used is a setting, CH_TTS:

    (not set) or off    no voice: the lecturer is read in captions only, each sentence
                        taking about as long as it would take to say.
    <module>:<Class>    any LiveKit text-to-speech class, such as
                        `livekit.plugins.cartesia:TTS` once that plugin is installed
                        (`uv add livekit-plugins-cartesia`). CH_TTS_OPTIONS is JSON
                        given to it as keyword arguments: the voice, the model. The
                        plugin reads its own key from the environment.
    mac                 on a Mac, its `say` command (`mac.py`), with CH_MAC_VOICE. It
                        costs nothing and needs no account: a stand-in for trying
                        things out, not a voice for real classes.
"""

from __future__ import annotations

import importlib
import json
import os

from livekit.agents import tts


def make_tts() -> tts.TTS | None:
    """The voice set by CH_TTS, or None if none is set: then the lecturer has no voice."""
    engine = os.environ.get("CH_TTS", "").strip()
    if engine.lower() in ("", "off", "none"):
        return None
    if engine == "mac":
        from .mac import MacSayTTS

        return MacSayTTS(voice=os.environ.get("CH_MAC_VOICE", "Samantha"))
    module, _, name = engine.partition(":")
    if not module or not name:
        raise ValueError(f"CH_TTS should be mac or <module>:<Class>, not {engine!r}")
    options = json.loads(os.environ.get("CH_TTS_OPTIONS", "") or "{}")
    voice = getattr(importlib.import_module(module), name)(**options)
    if not isinstance(voice, tts.TTS):
        raise ValueError(f"CH_TTS: {engine} is not a LiveKit text-to-speech class")
    return voice
