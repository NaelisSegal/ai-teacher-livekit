"""The class splitting into groups: who may split, who goes where, and coming back.

LiveKit is pretend here: rooms are a dictionary. The check with real rooms and real
lecturers is `dev/checks/groups_check.py`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, ClassVar

from fakes import PretendPerson, PretendRoom, PretendVoice, until

from lecturer.models.providers import Reply
from lecturer.room.events import ClassEvents
from lecturer.room.groups import GROUP_OF, Groups
from lecturer.script.steps import parse_steps
from lecturer.speech.decider import IN_GROUPS, VoiceDecider, utterances_from_steps
from lecturer.teaching.overseer import Overseer

STUDENT, HELPER, AGENT = "student", "helper", "agent"


@dataclass
class _LiveKit:
    """Rooms and the lecturers sent to them, as LiveKit would hold them."""

    rooms: dict[str, str] = field(default_factory=dict)  # name -> metadata
    sent: list[dict[str, Any]] = field(default_factory=list)  # what each lecturer was told

    def __post_init__(self) -> None:
        self.room = SimpleNamespace(
            create_room=self._create, list_rooms=self._list, update_room_metadata=self._update
        )
        self.agent_dispatch = SimpleNamespace(create_dispatch=self._dispatch)

    async def _create(self, request: Any) -> None:
        self.rooms[request.name] = request.metadata

    async def _dispatch(self, request: Any) -> None:
        self.sent.append({"room": request.room, **json.loads(request.metadata)})

    async def _update(self, request: Any) -> None:
        self.rooms[request.room] = request.metadata

    async def _list(self, request: Any) -> Any:
        found = [
            SimpleNamespace(name=n, metadata=m) for n, m in self.rooms.items() if n in request.names
        ]
        return SimpleNamespace(rooms=found)

    async def aclose(self) -> None:
        return None

    def say(self, room: str, **status: Any) -> None:
        """A group's lecturer reports how it is going."""
        self.rooms[room] = json.dumps({**json.loads(self.rooms[room]), "status": status})


def make(
    *people: PretendPerson, live: bool = True
) -> tuple[Groups, ClassEvents, _LiveKit, PretendRoom]:
    room = PretendRoom(*people)
    events = ClassEvents(room)  # type: ignore[arg-type]
    lk = _LiveKit()
    groups = Groups(
        room,  # type: ignore[arg-type]
        events,
        lesson="course/lesson",
        can_teach_live=live,
        lk=lk,
        keep_room=False,
    )
    groups.register()
    return groups, events, lk, room


CLASS = [
    PretendPerson("ada"),
    PretendPerson("bo"),
    PretendPerson("cy"),
    PretendPerson("hana", HELPER),
    PretendPerson("hugo", HELPER),
]
(SPLIT,) = parse_steps("split: random 8 | Again = Explain pairing again | On = Harder problems")


async def test_a_split_needs_a_helper_for_each_group() -> None:
    groups, events, lk, _ = make(PretendPerson("ada"), PretendPerson("hana", HELPER))
    why = await groups.open(SPLIT, [])
    assert "2 human helpers" in why and events.split is None and lk.rooms == {}
    # And a group taught from an instruction needs a model to teach it.
    groups, _, _, _ = make(*CLASS, live=False)
    assert "no writer model" in await groups.open(SPLIT, [])


async def test_each_group_is_a_room_with_a_lecturer_told_what_it_is_for() -> None:
    groups, events, lk, room = make(*CLASS)
    assert await groups.open(SPLIT, ["It pairs with T.", "Why is that?"]) == ""
    assert events.split is not None and groups.away
    first, _ = events.split["groups"]
    assert (first["title"], first["about"]) == ("Again", "Explain pairing again")
    assert (
        first["room"].startswith("class-1-g1-")
        and json.loads(lk.rooms[first["room"]])[GROUP_OF] == "class-1"
    )
    told = lk.sent[0]
    assert (
        told["lesson"] == "course/lesson" and told["live"] is True
    )  # this class's prep, taught live
    assert told["group"]["brief"] == "Explain pairing again" and told["group"]["main"] == "class-1"
    assert (
        told["group"]["said"] == ["It pairs with T.", "Why is that?"]
        and told["group"]["minutes"] == 8
    )
    # By chance: every student is put somewhere, helpers are not, and groups are even.
    to = events.split["to"]
    assert set(to) == {"ada", "bo", "cy"} and sorted(
        list(to.values()).count(g) for g in ("g1", "g2")
    ) == [1, 2]
    # Someone who arrives late is put in the smaller group.
    dee = PretendPerson("dee")
    room.remote_participants["dee"] = dee
    room.handlers["participant_connected"](dee)
    await until(lambda: "dee" in events.split["to"])  # type: ignore[index]
    assert sorted(list(events.split["to"].values()).count(g) for g in ("g1", "g2")) == [2, 2]
    # A helper may move a student, and nobody else may.
    assign = json.dumps({"action": "assign", "identity": "ada", "group": "g2"})
    await room.local_participant.rpc["ch.split"](
        SimpleNamespace(caller_identity="hana", payload=assign)
    )
    assert events.split["to"]["ada"] == "g2"
    try:
        await room.local_participant.rpc["ch.split"](
            SimpleNamespace(caller_identity="bo", payload=assign)
        )
        raise AssertionError("a student moved a student")
    except Exception as e:
        assert "staff" in str(e)
    await groups.shut()


async def test_groups_end_by_themselves_or_when_told_and_the_class_is_whole_again(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr("lecturer.room.groups.WATCH_SECONDS", 0.02)
    groups, events, lk, _ = make(*CLASS)
    done: list[str] = []
    groups.on_done = lambda: done.append("done")
    returning: list[bool] = []
    groups.on_returning = returning.append
    await groups.open(SPLIT, [])
    first, second = events.split["groups"]  # type: ignore[index]
    lk.say(first["room"], phase="teaching", minutes_in=2, students=2, last_said=["Look again."])
    await until(lambda: bool(groups.reports()[0].get("last_said")))
    # One finishes by itself.
    lk.say(first["room"], phase="ended")
    await until(lambda: events.split["groups"][0]["ended"])  # type: ignore[index]
    assert groups.open_now and not done
    # The other is told to end. Its lecturer sees `close` in its room, and says it has.
    closing = asyncio.create_task(groups.close("on"))
    await until(lambda: json.loads(lk.rooms[second["room"]]).get("close") is True)
    assert not groups.away  # from here an empty main room is waited in, not taught to
    lk.say(second["room"], phase="ended")
    await closing
    await until(lambda: events.split is None)
    assert done == ["done"] and [r["title"] for r in groups.last] == ["Again", "On"]
    assert returning[:2] == [True, True]  # told to wait for them when called, and when over
    await groups.shut()


async def test_one_student_is_taken_aside_only_if_a_helper_agrees() -> None:
    (aside,) = parse_steps("split: one ada 4 | A word = Go through the last step with them")
    groups, events, lk, room = make(*CLASS)
    answer = room.local_participant.rpc["ch.split"]

    async def helper_says(ok: bool) -> str:
        asking = asyncio.create_task(groups.aside(aside, []))
        await until(lambda: bool(events.asks))
        ask = events.asks[0]
        assert ask["identity"] == "ada" and "the last step" in ask["why"]
        reply = json.dumps({"action": "answer", "id": ask["id"], "ok": ok})
        await answer(SimpleNamespace(caller_identity="hugo", payload=reply))
        return await asking

    assert "no helper agreed" in await helper_says(False)
    assert events.split is None and lk.rooms == {} and events.asks == []
    assert await helper_says(True) == ""
    assert events.split is not None and events.split["how"] == "one"
    assert events.split["to"] == {"ada": "g1"} and not groups.away  # the class goes on
    assert lk.sent[0]["group"]["private"] is True
    assert groups.reports()[0]["private"] and "last_said" not in groups.reports()[0]
    await groups.shut()
    # With one helper the class would be left alone, so it is not done.
    groups, _, _, _ = make(PretendPerson("ada"), PretendPerson("hana", HELPER))
    assert "2 human helpers" in await groups.aside(aside, [])


async def test_the_overseer_may_end_a_group_and_leaves_working_ones_alone(monkeypatch: Any) -> None:
    monkeypatch.setattr("lecturer.room.groups.WATCH_SECONDS", 0.02)
    monkeypatch.setattr("lecturer.room.groups.CLOSE_WAIT", 0.2)
    groups, events, lk, _ = make(*CLASS)
    await groups.open(SPLIT, [])
    first, second = events.split["groups"]  # type: ignore[index]
    lk.say(
        first["room"], phase="teaching", minutes_in=3, students=0, last_said=["Is anyone there?"]
    )
    lk.say(second["room"], phase="teaching", minutes_in=3, students=3, last_said=["Try this one."])
    await until(lambda: all(r.get("last_said") for r in groups.reports()))

    class Model:
        name = "pretend"
        shown = ""
        answer: ClassVar[dict[str, Any]] = {"end": [], "why": "both are working"}

        async def complete(self, _system: str, messages: Any, **_: Any) -> Reply:
            self.shown = messages[0]["content"]
            return Reply(json.dumps(self.answer))

    model = Model()
    overseer = Overseer(groups, model, events)  # type: ignore[arg-type]
    assert await overseer.look() == []
    assert '"Again" (3 min in, 0 students)' in model.shown and "Try this one." in model.shown
    model.answer = {"end": ["again", "No such group"], "why": "nobody is in it"}
    assert await overseer.look() == ["Again"]
    assert json.loads(lk.rooms[first["room"]]).get("close") is True
    assert not json.loads(lk.rooms[second["room"]]).get("close")
    await groups.shut()


async def test_the_main_lecturer_waits_while_the_class_is_in_groups(monkeypatch: Any) -> None:
    monkeypatch.setattr("lecturer.room.groups.WATCH_SECONDS", 0.02)
    steps = parse_steps(
        "say: Into groups now.\nsplit: choose 5 | A = x/y | B = Do b\nsay: Welcome back."
    )
    _, wait, back, after = utterances_from_steps(steps, "seg")
    assert (wait.pause, wait.pause_text, wait.split) == (300, IN_GROUPS, steps[1])
    assert [s.verb for s in back.board] == ["regroup"] and after.text == "Welcome back."

    groups, events, lk, _ = make(*CLASS)
    decider = VoiceDecider(PretendVoice(), events, gap_ms=0)  # type: ignore[arg-type]
    decider.groups = groups
    groups.on_done = decider.end_groups_wait
    heard: list[str] = []
    decider.listeners.append(
        lambda what, utt, _: heard.append(utt.text) if what == "heard" else None
    )
    voice = asyncio.create_task(decider.run())
    decider.enqueue_steps(steps, source="script")
    await until(lambda: events.split is not None)
    await asyncio.sleep(0.2)
    assert heard == ["Into groups now."] and events.pause is not None  # waiting, not teaching
    # What ends a thinking pause (a student pressing done, the teacher writing something
    # else) does not call a class back from its groups, and what is taken back does
    # not include the step that will.
    decider.end_pause()
    assert [u.text for u in decider.drop_unspoken("script")] == ["Welcome back."]
    await asyncio.sleep(0.2)
    assert events.split is not None and events.pause is not None
    decider.enqueue_steps(parse_steps("say: Welcome back."), source="script")
    for group in events.split["groups"]:  # type: ignore[index]
        lk.say(group["room"], phase="ended")
    await until(lambda: "Welcome back." in heard)  # long before the five minutes are up
    assert events.split is None
    voice.cancel()
    await groups.shut()

    # With no helpers there is no split and no wait: the class carries on together.
    groups, events, _, _ = make(PretendPerson("ada"))
    decider = VoiceDecider(PretendVoice(), events, gap_ms=0)  # type: ignore[arg-type]
    decider.groups = groups
    heard = []
    decider.listeners.append(
        lambda what, utt, _: heard.append(utt.text) if what == "heard" else None
    )
    voice = asyncio.create_task(decider.run())
    decider.enqueue_steps(steps, source="script")
    await until(lambda: "Welcome back." in heard)
    assert events.split is None
    voice.cancel()


async def test_only_staff_are_told_who_may_be_taken_aside_and_why() -> None:
    sent: list[tuple[dict[str, Any], list[str] | None]] = []

    async def send_text(text: str, **kw: Any) -> None:
        sent.append((json.loads(text), kw.get("destination_identities")))

    people = [PretendPerson("ada", name="Ada"), PretendPerson("bo", name="Bo"), CLASS[3], CLASS[4]]
    groups, events, _lk, room = make(*people)
    room.local_participant.send_text = send_text
    (step,) = parse_steps("split: one ada 4 | A word = Go through the last step with them")
    asking = asyncio.create_task(groups.aside(step, []))
    await until(lambda: bool(events.asks))
    asks = [(m, to) for m, to in sent for ev in m.get("events", []) if ev["type"] == "split.ask"]
    noops = [(m, to) for m, to in sent for ev in m.get("events", []) if ev["type"] == "noop"]
    assert asks and all(to and "ada" not in to and "bo" not in to for _, to in asks)
    assert noops and all(to and set(to) <= {"ada", "bo"} for _, to in noops)
    # A student's snapshot has no asks; a helper's has.
    assert events.snapshot("bo")["asks"] == [] and events.snapshot("hana")["asks"]
    asking.cancel()
