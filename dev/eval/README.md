# Evaluation tools (optional)

Tools for finding out how well the live teacher teaches, and what each part of it
adds. Nothing else in the repo depends on them: a lecturer runs without them, and
the checks in `dev/checks/` do not use them.

- `sim_class.py`: a simulated live class, with a pretend voice and pretend
  students, faster than real time. Students follow a list of events, or are played
  by a model (`students.py`, set by `CH_STUDENTS`) from a short description.
- `ai_student.py`: an AI student, played the same way, who joins a real class
  beside real people.
- `from_record.py`: a simulated class built from a real one, with students who act
  like the real ones did. Needs a class taught with `CH_RECORD_CLASS=1`, which keeps
  what students typed and did: use it only with their agreement.
- `compare.py`: one simulated class run several ways (with or without the planner,
  the decision model, another writer model) and compared, with an optional model
  judge.

They need a model key and a lesson of your own, and each simulated class costs
about what a real one does. How each is used is at the top of its file.
