# AI teacher for LiveKit

An AI lecturer that teaches live online classes. Students join in a browser and the
lecturer teaches them in real time: it speaks, writes on a board as it speaks, shows
slides, pictures, videos and web pages, sets the class problems and waits while they
work, answers what they type or say, and changes the lesson to fit the people who
are there. 

It is what will teach the classes at [Curiosity Hall](https://curiosityhall.com).

This is an early open-source project, and it will change a lot.

## Built on LiveKit

LiveKit does the real-time work, and the lecturer is an ordinary LiveKit agent:

- **A class is a LiveKit room.** The web app signs each person's join token, with
  their role (student, helper or guest) in it, and sends for the lecturer with an
  agent dispatch when the first person arrives.
- **The lecturer is a [LiveKit Agents](https://docs.livekit.io/agents/) worker.** It
  speaks on one audio track, through any LiveKit text-to-speech plugin, and shares a
  browser as a video track.
- **Everything else goes over LiveKit's data channels.** The board, the stage and
  captions go out as events on a text stream; chat and reactions have topics of their
  own; raising a hand, voting and catching up after joining late are RPCs.
- **Microphones are LiveKit permissions.** Students join unable to publish, and the
  lecturer grants a student the microphone through the server API when they have the
  floor, so a browser cannot give itself one. Active speakers tell it when to stop
  talking.
- **Groups are rooms of their own**, each with its own dispatched lecturer, which
  talk to the main class through room metadata.

## How it works

A lesson is prepared ahead as a script, teaching notes and slides. A class can be:

- **Taught live.** A model (the writer) writes the class a few sentences ahead of the
  voice, from the lesson prep, and changes course from what the class does: it
  slows down, goes over a step again, gives a harder problem, takes a question, or
  solves a problem a student brings. A fast decision model sorts chat messages (is
  this a question? an answer? is it right?), and a planner can reshape the rest of
  the class at checkpoints. Both are optional.
- **Read from its script**, as written. This needs no models at all, and it is
  what a live class falls back on if the writer fails.

What students see is sent as events, so every browser draws the board and the stage
itself, and someone who joins late is caught up from a snapshot.

## Features

- **The board:** maths in LaTeX, drawn as it is said; pages that can be brought back;
  marks, two columns, pictures.
- **The stage:** the board, slides, a shared screen (a real browser, sent as video)
  or a video, alone or side by side.
- **Pictures and video:** pictures from lesson prep, or made during the class by an
  image model; videos (files, YouTube or Twitch) played in each browser, in step
  for everyone.
- **Working on problems:** thinking time that ends when everyone has finished,
  answers in the chat marked right or not yet, polls, and thumbs up, down and done.
- **Who may speak:** hands raised and the floor given in turn, or open mics. Helpers
  and guest lecturers can speak and share a screen at any time, and the lecturer
  goes quiet while a person talks. With a model for it, in a live class, what is
  said aloud is written down and answered like chat.
- **Groups:** the class can split into groups, each in its own room with its own
  lecturer, chosen by students, by chance or by a helper, and come back together. A
  student can be taken aside, if a helper agrees. Another model can keep an eye on
  the groups and end one early.
- **Students:** known to the teacher by name, or by label only, for privacy
  (`CH_STUDENT_NAMES`). Someone in two tabs is one person, and is asked, as in a
  meeting app, whether to switch tabs or join in both.
- **Any model:** each job (writing, planning, deciding, hearing, pictures) names its
  model as `provider:model`: Anthropic, OpenAI, OpenRouter, or any server that speaks
  the OpenAI API. A job with no model is simply off.
- **Any voice:** any LiveKit text-to-speech plugin, the Mac's `say` for trying
  things out, or none (captions only).
- **Your own teaching style:** the prompts are one way to teach, and you can use your
  own (`CH_PROMPTS_DIR`).
- **Privacy:** the class log keeps what students heard and saw, never what they
  typed or said. Students are told which AI services their words may be sent to.
- **Optional evaluation tools:** simulated classes with model-played students, an AI
  student who joins a real class, and a way to run one class with different
  settings and compare (`dev/eval/`).

## Running it

You need [`livekit-server`](https://docs.livekit.io/home/self-hosting/local/),
[`uv`](https://docs.astral.sh/uv/) and [`pnpm`](https://pnpm.io/) (with Node 20.9 or
later). From the top of the repo:

    (cd agent && uv sync && cp .env.example .env.local)
    (cd web && pnpm install && cp .env.example .env.local)
    dev/stack.sh

Then open http://localhost:4072. It lists every lesson it finds, with a link to
join each one (a new class starts a few seconds after you join), and to join as
the helper. Try the example lesson, `example/cone`.

Out of the box there are no models and no voice: classes are read from their
scripts, in captions. To have a class taught live, put a key and a writer model in
`agent/.env.local`, for example:

    ANTHROPIC_API_KEY=...
    CH_WRITER=anthropic:claude-sonnet-5-5

and use a lesson's "taught live" link. `agent/.env.example` lists every setting, and

    cd agent && uv run python -m lecturer.settings --try

shows what each job is set to and tries each model once.

Ports: 4070 LiveKit, 4071 media, 4072 web.

### A voice

Any LiveKit text-to-speech plugin works. For example, OpenAI's:

    (cd agent && uv sync --extra openai)

and in `agent/.env.local`:

    OPENAI_API_KEY=...
    CH_TTS=livekit.plugins.openai:TTS
    CH_TTS_OPTIONS={"model": "gpt-4o-mini-tts", "voice": "ash"}

For another plugin, install it (`cd agent && uv add livekit-plugins-<name>`) and name
its class the same way. On a Mac, `CH_TTS=mac` uses the built-in `say` command, free,
for trying things out.

### With Docker

    cp agent/.env.example agent/.env.local
    cp web/.env.example web/.env.local
    docker compose up --build

starts LiveKit, the lecturer and the web app (with the OpenAI voice plugin installed)
on the same ports, sharing `lessons/`, `generated/` and `logs/` with this folder. The
lecturer's image has no Chrome, so a lesson's shared-screen steps are skipped.

## Writing a lesson

A lesson is a few text files in `lessons/<course>/`: a script of say, write and show
steps, divided into parts, with teaching notes and slides beside it. The example in
`lessons/example/` is a good start, `lessons/README.md` says what goes where, and the
script format is at the top of `agent/src/lecturer/script/steps.py`. To check one:

    cd agent && uv run python -m lecturer.check <course>/<lesson>

## Running it on a server

It runs on one server, as the lecturer and the web app share folders on disk
(`lessons/` and `generated/`), and the web app keeps its join limits in memory, so
run one web process. The web app on a serverless host, or the lecturer on LiveKit
Cloud's agent hosting, are not supported yet.

- Run the web app built (`cd web && pnpm build && pnpm start --port 4072`), and the
  lecturer with `start`, not `dev` (`cd agent && uv run python -m lecturer.agent
  start`), or use `docker-compose.yml`. Use your own LiveKit server or LiveKit Cloud
  for the rooms: set `LIVEKIT_URL`, `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` for
  both, and `LIVEKIT_PUBLIC_URL` for the web app if browsers reach LiveKit at another
  address. With Docker, set `node_ip` in `dev/livekit.docker.yaml` to the server's
  public address.
- Leave `CH_OPEN_ROOMS` out. Then a class can be joined only if `lessons/schedule.json`
  lists it, and only around its time, at `/class/<its id>`. Nothing lists the
  classes: give each class's link to the people who should come. Each class can be
  taught live or read from its script (`"live": true` in the schedule).
- Set `NEXT_PUBLIC_SITE_NAME` to what your classes are called.
- Set long random codes for helpers and guest lecturers (`CH_HELPER_CODE`,
  `CH_GUEST_CODE` in `web/.env.local`), and `CH_PROXY_HOPS` if the web app is behind a
  proxy.
- The lecturer and the web app must see the same `lessons/` and `generated/` folders.
- The shared screen opens web pages in a real browser. Run it somewhere with nothing
  private to reach (see `agent/src/lecturer/room/screen.py`).
- We recommend a human helper for each group when a class splits
  (`CH_SPLIT_HELPERS`, 1 by default).

## Layout

- `agent/`: the lecturer, a LiveKit agent in Python (`src/lecturer/`; its words are
  explained at the top of `src/lecturer/__init__.py`).
  - `agent.py`: starts a class and wires the parts together; `job.py`: what a
    lecturer is told when it is sent to a room; `settings.py`: which model does
    which job; `check.py`: the lesson check.
  - `script/`: the lesson format: scripts (`steps.py`, `lesson.py`), slides
    (`deck.py`), pictures and videos (`media.py`).
  - `room/`: what the class sees and does: the event stream and log, board, stage,
    shared screen, chat, the floor, reactions, polls, pictures, groups.
  - `speech/`: the speech queue (`decider.py`), the voice, and hearing.
  - `teaching/`: the live teacher (`teacher.py`), its prompts, the groups' overseer,
    and chat answers for a class read from its script.
  - `models/`: what is asked of each model, and the services they are reached through.
- `web/`: the class page, a Next.js app (`app/class/`, `lib/classroom/`).
- `lessons/`: lessons and the schedule; only the example is in this repo.
- `dev/`: the local stack, real-room checks with their test lessons (`checks/`), and
  the optional evaluation tools (`eval/`).
- `docker-compose.yml`, `agent/Dockerfile`, `web/Dockerfile`: the same, in Docker.

## Checks

    (cd agent && uv run pytest && uv run ruff check . ../dev)
    (cd web && pnpm test && pnpm lint)

The same run on every push (`.github/workflows/checks.yml`). With the stack running,
checks in a real room:

    cd agent
    uv run python ../dev/checks/student_bot.py check-1 --lesson test/pages
    PYTHONPATH=../dev/checks uv run python ../dev/checks/floor_check.py floor-1
    PYTHONPATH=../dev/checks uv run python ../dev/checks/groups_check.py
    PYTHONPATH=../dev/checks uv run python ../dev/checks/media_check.py
    PYTHONPATH=../dev/checks uv run python ../dev/checks/hearing_check.py speech.wav

The last needs `CH_HEARING`, `CH_WRITER` and a recording of someone speaking.

## Known gaps

- Helper and guest codes are put in page addresses, and a class has no password of
  its own: anyone with its link can join.
- Model call limits are for each lecturer, so a class that splits into groups can
  make more calls, and the groups' overseer is not counted.
- One server only: no serverless hosting or LiveKit Cloud agents yet (see above).
- Moderation is not built.

## Licence

[AGPL-3.0](LICENSE): you may use, change and share this, and if you run a changed
version for other people to use, you must make your changes available under the same
licence. `agent/` started from
[agent-starter-python](https://github.com/livekit-examples/agent-starter-python)
(MIT) and `web/` from [meet](https://github.com/livekit-examples/meet)
(Apache-2.0); their licence files are kept beside the code.
