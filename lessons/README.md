# Lessons

Your lessons go here. There is one example, `example/cone`: a short class that can be
read from its script or taught live.

    lessons/<course>/<lesson>.seg        the script (format: agent/src/lecturer/script/steps.py and lesson.py)
    lessons/<course>/<lesson>.notes.md   teaching notes for the live teacher (optional)
    lessons/<course>/<deck>.deck         slides (format: agent/src/lecturer/script/deck.py)
    lessons/<course>/<file>              pictures and videos the script or slides use
    lessons/schedule.json                the classes this server runs (shape: `Session` in web/lib/schedule.ts)

`<course>` is only a folder name, to keep a subject's lessons together.

On a real server, a class can be joined only if `schedule.json` has it, from ten
minutes before it starts to fifteen after it ends, at `/class/<its id>`. The schedule is
not shown to anyone: give each class's link to the people who should come. In
development (`CH_OPEN_ROOMS=1`) any room name works, and the lesson is chosen by the
link; the development front page lists every lesson. An example `schedule.json`:

    {"sessions": [{"id": "cone-tuesday", "lesson": "example/cone", "title": "Why a cone is a third",
                   "starts": "2026-12-01T19:00:00+11:00", "minutes": 15}]}

To check a lesson (that its script and slides read, how long it takes, that the files
it uses are there, and whether it can be taught live):

    cd agent && uv run python -m lecturer.check <course>/<lesson>

To keep lessons somewhere else, set `CH_LESSONS_DIR` for both the lecturer and the web
app.

The short test lessons that the checks use are in `dev/checks/lessons/`.
