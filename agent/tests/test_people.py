"""Someone in the class in two tabs (or windows) is one person: one vote, one hand,
one student."""

from __future__ import annotations

import json
from types import SimpleNamespace

from fakes import PretendPerson, PretendRoom, PretendVoice

from lecturer.room.events import ClassEvents
from lecturer.room.floor import Floor, person_of
from lecturer.room.polls import Polls
from lecturer.speech.decider import VoiceDecider

# Ada in two tabs: her own identity, and a second tab that says it is hers.
ADA = PretendPerson("ada__x1", name="Ada")
ADA_TOO = PretendPerson("ada__x1__tq7k2", name="Ada", person="ada__x1")
BO = PretendPerson("bo__x2", name="Bo")


def call(caller: str, payload: str) -> SimpleNamespace:
    return SimpleNamespace(caller_identity=caller, payload=payload)


def test_a_second_tab_is_the_same_person() -> None:
    assert person_of(ADA) == person_of(ADA_TOO) == "ada__x1"
    assert person_of(BO) == "bo__x2"
    assert person_of(None, "someone") == "someone"


async def test_one_vote_for_each_person() -> None:
    room = PretendRoom(ADA, ADA_TOO, BO)
    events = ClassEvents(room)  # type: ignore[arg-type]
    polls = Polls(room, events)  # type: ignore[arg-type]
    polls.register()
    await polls.open("Which?", ["this", "that"])
    vote = room.local_participant.rpc["ch.vote"]
    for who, option in (("ada__x1", 0), ("ada__x1__tq7k2", 1), ("bo__x2", 1)):
        assert await vote(call(who, json.dumps({"poll": polls._id, "option": option}))) == "ok"
    # Ada's second vote replaced her first: two people, two votes.
    assert sorted(polls._counts()) == [0, 2]


async def test_one_hand_for_each_person() -> None:
    room = PretendRoom(ADA, ADA_TOO, BO)
    events = ClassEvents(room)  # type: ignore[arg-type]
    decider = VoiceDecider(PretendVoice(), events, gap_ms=0)  # type: ignore[arg-type]
    floor = Floor(room, events, decider)  # type: ignore[arg-type]
    floor._caller = lambda data: room.remote_participants.get(data.caller_identity)  # type: ignore[method-assign]
    assert await floor._on_hand(call("ada__x1", "up")) == "ok"
    assert await floor._on_hand(call("ada__x1__tq7k2", "up")) == "ok"
    assert len(events.floor.hands) == 1
    # Lowered from the other tab, it comes down.
    assert await floor._on_hand(call("ada__x1__tq7k2", "down")) == "ok"
    assert events.floor.hands == []
