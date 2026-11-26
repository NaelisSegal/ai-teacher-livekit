"""The lecturer: a LiveKit agent that teaches a class live.

Words this code uses, and what they mean:

    lesson        `<course>/<lesson>`: a script (`.seg`), with teaching notes and slides
                  beside it. The lesson prep is all of that together (`script/lesson.py`).
    script        a lesson's say / write / mark steps (`script/steps.py`). A live class is
                  written as it is taught, in the same steps; the script is its fallback.
    part          a stretch of a script, must or optional, with its minutes.
    the writer    the model that writes a live class a few sentences ahead of the voice.
    the planner   the model that reshapes the rest of a live class at checkpoints.
    the decision model   a fast model that answers yes-or-no questions about chat
                  messages ("is this an answer?"), as probabilities (`models/decisions.py`).
    speech queue  `speech/decider.py`: the one queue of everything said, with the board
                  and stage steps that go with each sentence, its pauses and its holds.
    hold          the voice stopped until a reason lifts (`speech/holds.py`).
    wait          a pause the teacher set for the class to work, and what they did in it.
    the floor     who may speak: hands raised, the mic given, or open mics (`room/floor.py`).
    hearing       speech to text: what people say aloud, written down (`speech/hearing.py`).
    events        everything students see, in order; a browser joining late is sent a
                  snapshot (`room/events.py`). The browser keeps the same state by the
                  same rules (`web/lib/classroom/store.ts`).
    stage         what is on screen: the board, slides, a shared screen or a video.
    group         a room of its own, with a lecturer of its own, when a class splits
                  (`room/groups.py`). The overseer is a model that watches the groups.
    keeper        a hidden connection that keeps a class's room open while everyone is
                  away in groups.
    helper        a person who supervises a class (staff); a guest lecturer is the other
                  staff role.
    person        who a student is: someone in two tabs is one person (`ch.person`).
"""
