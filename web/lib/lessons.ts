// Where lessons are. Server side only.
//
// Nothing here names a lesson. A lesson is `<course>/<lesson>`, found in the lessons
// folders: `lessons/` at the top of the repo, or the folders listed in CH_LESSONS_DIR
// (several are separated by `:`, and the first that has the file wins). The lecturer
// looks in the same places (`agent/src/lecturer/script/lesson.py`).

import { access, readdir, readFile } from 'node:fs/promises';
import path from 'node:path';

export const LESSONS_DIRS: string[] = (
  process.env.CH_LESSONS_DIR || path.join(process.cwd(), '..', 'lessons')
)
  .split(path.delimiter)
  .filter(Boolean)
  .map((dir) => path.resolve(dir));

/** The full path of a file in a lessons folder, or null if no folder has it. */
export async function findLessonFile(...parts: string[]): Promise<string | null> {
  for (const dir of LESSONS_DIRS) {
    const file = path.resolve(dir, ...parts);
    // Never outside the folder, whatever the parts are.
    if (!file.startsWith(dir + path.sep)) continue;
    const there = await access(file).then(
      () => true,
      () => false,
    );
    if (there) return file;
  }
  return null;
}

export type LessonListing = {
  /** `<course>/<lesson>` */
  name: string;
  title: string;
  /** From the script's header, or 0 if it does not say. */
  minutes: number;
  /** The script has `part:` lines, which the live teacher needs. */
  parts: boolean;
};

const PLAIN = /^[a-z0-9-]+$/;

/** Every lesson script in the lessons folders. For the development index. */
export async function listLessons(): Promise<LessonListing[]> {
  const found = new Map<string, LessonListing>();
  for (const dir of LESSONS_DIRS) {
    for (const course of await readdir(dir, { withFileTypes: true }).catch(() => [])) {
      if (!course.isDirectory() || !PLAIN.test(course.name)) continue;
      for (const file of await readdir(path.join(dir, course.name)).catch(() => [])) {
        const lesson = file.endsWith('.seg') ? file.slice(0, -4) : '';
        const name = `${course.name}/${lesson}`;
        if (!PLAIN.test(lesson) || found.has(name)) continue;
        const text = await readFile(path.join(dir, course.name, file), 'utf8').catch(() => '');
        const head = text.split(/^---\s*$/m)[1] ?? '';
        const field = (key: string) =>
          new RegExp(`^${key}:\\s*(.+)$`, 'm').exec(head)?.[1].trim() ?? '';
        found.set(name, {
          name,
          title: field('title') || name,
          minutes: Number(field('minutes')) || 0,
          parts: /^\s*part:/m.test(text),
        });
      }
    }
  }
  return [...found.values()].sort((a, b) => a.name.localeCompare(b.name));
}
