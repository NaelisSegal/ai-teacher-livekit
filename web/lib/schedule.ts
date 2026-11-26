// The schedule. Server side only: read from the lessons folders.
//
// A session is one thing that happens at a set time: a class or office hours. Its id
// is also its room name, so a room can exist only if the schedule has it.

import { findLessonFile } from './lessons';
import { readFile } from 'node:fs/promises';

/** Development only: any room name may be opened, and a made-up schedule is used. */
export const OPEN_ROOMS = process.env.CH_OPEN_ROOMS === '1';

/** Doors open this long before a session, and stay open this long after it. */
const EARLY_MS = 10 * 60_000;
const LATE_MS = 15 * 60_000;

export type SessionKind = 'class' | 'office-hours';

export type Session = {
  /** Also the room name. */
  id: string;
  kind: SessionKind;
  title: string;
  about: string;
  lesson: string;
  /** When it starts, as milliseconds since 1970. */
  starts: number;
  minutes: number;
};

export type SessionState = 'soon' | 'open' | 'over';

const started = Date.now();

/** "2026-12-02T19:00:00+11:00", or in the dev schedule "now+2h", "now-1m", "now-2d". */
function parseStart(text: string): number {
  const relative = /^now([+-]\d+)([mhd])$/.exec(text);
  if (relative && OPEN_ROOMS) {
    const unit = { m: 60_000, h: 3_600_000, d: 86_400_000 }[relative[2] as 'm' | 'h' | 'd'];
    return started + Number(relative[1]) * unit;
  }
  const at = Date.parse(text);
  if (Number.isNaN(at)) throw new Error(`bad start time in the schedule: ${text}`);
  return at;
}

async function readJson<T>(...parts: string[]): Promise<T | null> {
  try {
    const file = await findLessonFile(...parts);
    return file ? (JSON.parse(await readFile(file, 'utf8')) as T) : null;
  } catch {
    return null;
  }
}

export async function loadSessions(): Promise<Session[]> {
  const file = OPEN_ROOMS ? 'schedule.dev.json' : 'schedule.json';
  const data = await readJson<{ sessions: unknown[] }>(file);
  const sessions: Session[] = [];
  // Each entry is checked on its own. A bad one is left out and reported, so that one
  // mistake in the file does not stop every class from being joined.
  for (const raw of Array.isArray(data?.sessions) ? data.sessions : []) {
    const s = (raw ?? {}) as Record<string, unknown>;
    try {
      const id = String(s.id ?? '');
      const lesson = String(s.lesson ?? '');
      const minutes = Number(s.minutes);
      if (!/^[\w-]{1,64}$/.test(id)) throw new Error('bad id');
      if (!/^[a-z0-9-]+\/[a-z0-9-]+$/.test(lesson)) throw new Error('bad lesson');
      if (!(minutes > 0 && minutes <= 600)) throw new Error('bad minutes');
      sessions.push({
        id,
        kind: s.kind === 'office-hours' ? 'office-hours' : 'class',
        title: String(s.title ?? id),
        about: String(s.about ?? ''),
        lesson,
        starts: parseStart(String(s.starts ?? '')),
        minutes,
      });
    } catch (e) {
      console.error(
        `schedule entry left out (${String(s.id)}):`,
        e instanceof Error ? e.message : e,
      );
    }
  }
  return sessions.sort((a, b) => a.starts - b.starts);
}

export function stateOf(session: Session, now: number = Date.now()): SessionState {
  if (now < session.starts - EARLY_MS) return 'soon';
  if (now > session.starts + session.minutes * 60_000 + LATE_MS) return 'over';
  return 'open';
}

export async function findSession(id: string): Promise<Session | undefined> {
  return (await loadSessions()).find((s) => s.id === id);
}

export const KIND_NAMES: Record<SessionKind, string> = {
  class: 'Class',
  'office-hours': 'Office hours',
};

/** The time now. In a function so that pages do not read the clock while drawing. */
export function nowMs(): number {
  return Date.now();
}
