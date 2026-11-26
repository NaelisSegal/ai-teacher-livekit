"""Why the lecturer's voice may be held (`VoiceDecider.hold`): it says nothing more
until every hold is lifted. Kept apart, so that anything may hold the voice without
importing the speech queue."""


class Hold:
    EMPTY_ROOM = "empty room"  # nobody is here to teach (agent.py)
    FLOOR = "floor"  # a student has been given the floor (room/floor.py)
    SPEAKING = "someone speaking"  # a person is talking (room/floor.py)
    HELPER = "helper"  # a helper has paused the lecturer (room/floor.py)
    GROUPS_RETURNING = "groups returning"  # the class is coming back from groups (agent.py)
