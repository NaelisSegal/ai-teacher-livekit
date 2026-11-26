// Started from livekit-examples/meet (Apache-2.0): app/api/connection-details/route.ts.
// Changes: students cannot publish audio or video when they join (the microphone is
// only for a student who has been given the floor), a room can be joined only if the
// schedule has it, joining is rate-limited, and the lecturer agent is sent to the
// room if it has not been already.

import { findLessonFile } from '@/lib/lessons';
import { findSession, nowMs, OPEN_ROOMS, stateOf } from '@/lib/schedule';
import type { ConnectionDetails } from '@/lib/types';
import {
  AccessToken,
  AgentDispatchClient,
  RoomServiceClient,
  TrackSource,
  VideoGrant,
} from 'livekit-server-sdk';
import { NextRequest, NextResponse } from 'next/server';
import { createHmac } from 'node:crypto';

const API_KEY = process.env.LIVEKIT_API_KEY;
const API_SECRET = process.env.LIVEKIT_API_SECRET;
const LIVEKIT_URL = process.env.LIVEKIT_URL;

const LECTURER = 'lecturer';
const COOKIE_KEY = 'ch-participant-postfix';

/** Letters (with their accents and marks), numbers, spaces, hyphens, apostrophes and
 *  full stops, in any language. */
const NAME = /^[\p{L}\p{N}][\p{L}\p{M}\p{N} .'\-]{0,29}$/u;

const say = (text: string, status: number) => new NextResponse(text, { status });
const JOIN_WINDOW = 5 * 60_000; // joins are counted over five minutes
const CODE_WINDOW = 10 * 60_000; // wrong staff codes, over ten

/** What a lecturer is told when it is sent to a room. The full list of keys, and what
 *  each means, is agent/src/lecturer/job.py; this side sends only these. */
type LecturerJob = {
  lesson?: string;
  /** Written as it is taught, not read from the script (development: `live-` rooms). */
  live?: boolean;
  /** Office hours: keep the room open this long. */
  stay_seconds?: number;
};

// Joining is a POST, so that names and staff codes are not in this request's address,
// which web servers and proxies write to their logs. (A helper's link to the class page
// does still carry their code, `?as=helper&code=`: a known gap, in the README.)
export async function POST(request: NextRequest) {
  try {
    if (!LIVEKIT_URL || !API_KEY || !API_SECRET) {
      throw new Error('LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set');
    }
    const body = (await request.json().catch(() => null)) as Record<string, unknown> | null;
    const text = (key: string) => (typeof body?.[key] === 'string' ? (body[key] as string) : '');
    const roomName = text('roomName');
    // Already in this class in another tab or window: 'switch' moves here (the other is
    // dropped), 'also' joins here as well. Neither: we ask, as a meeting app does.
    const tabs = text('tabs');
    const participantName = text('participantName').normalize('NFC').trim().replace(/\s+/g, ' ');
    if (!/^[\w-]{1,64}$/.test(roomName)) return say('Bad or missing roomName', 400);
    if (!NAME.test(participantName)) {
      return say(
        'Names can use letters, numbers, spaces, hyphens and apostrophes, up to 30 characters.',
        400,
      );
    }
    const from = addressOf(request);
    // One address is checked first, so that one sender who is refused cannot use up
    // the room that everyone shares.
    if (
      (from && !notTooOften(joins, from, 60, JOIN_WINDOW)) ||
      !notTooOften(joins, 'all', 600, JOIN_WINDOW)
    ) {
      return say('Too many people are joining from here at once. Wait a few minutes.', 429);
    }

    // A room can be joined only if the schedule has it and it is open now. Every new
    // room sends for a lecturer, so rooms are not made on demand. (In development,
    // with CH_OPEN_ROOMS=1, any room name works and the lesson can be chosen.)
    const session = await findSession(roomName);
    const lecturer: LecturerJob = {};
    // A group of a class that has split is a room the class's lecturer made, with a
    // lecturer already sent for. It is joined as it is.
    const groupRoom = session ? null : await groupRoomOf(roomName);
    const group = groupRoom !== null;
    if (group) {
      // Nothing to choose and nobody to send for.
    } else if (session) {
      const state = stateOf(session);
      if (state === 'soon') {
        return say('This has not opened yet. The room opens 10 minutes before the start.', 403);
      }
      if (state === 'over') return say('This session is over.', 403);
      lecturer.lesson = session.lesson;
      if (session.kind === 'office-hours') {
        // Office hours have no script to get through: the room stays open to the end.
        const end = session.starts + session.minutes * 60_000;
        lecturer.stay_seconds = Math.max(0, Math.round((end - nowMs()) / 1000));
      }
    } else if (OPEN_ROOMS) {
      lecturer.lesson = text('lesson');
      // In development, a room whose name starts with "live-" is taught by the live
      // teacher (written as it is taught) instead of read from the script.
      if (roomName.startsWith('live-')) lecturer.live = true;
    } else {
      return say('There is no class with this name.', 404);
    }
    const lesson = String(lecturer.lesson);
    if (!group && (!/^[a-z0-9-]+\/[a-z0-9-]+$/.test(lesson) || !(await lessonExists(lesson)))) {
      return say('This class has no lesson to teach yet.', 400);
    }

    // A helper or guest lecturer joins with a code. Everyone else is a student.
    const as = text('as');
    const wantsStaff = as === 'helper' || as === 'guest';
    // Wrong codes are counted for each address if it is known (CH_PROXY_HOPS), and
    // otherwise for everyone together, with room for many more, so that a few
    // students trying codes cannot lock every helper out.
    const codeLimit = from ? 5 : 50;
    if (wantsStaff && !notTooOften(wrongCodes, from ?? 'all', codeLimit, CODE_WINDOW, false)) {
      return say('Too many wrong codes. Wait ten minutes.', 429);
    }
    const role = staffRole(as, text('code'));
    if (role === null) {
      notTooOften(wrongCodes, from ?? 'all', codeLimit, CODE_WINDOW);
      return say('That code is not right', 403);
    }

    if (!group) await ensureLecturer(roomName, lecturer);

    // The identity's tail comes from a cookie we set and signed. Anything else is
    // replaced, so nobody can join as someone whose identity they have only seen.
    const [tail, mark] = (request.cookies.get(COOKIE_KEY)?.value ?? '').split('.');
    const postfix = /^[a-z0-9]{6}$/.test(tail) && mark === signed(tail) ? tail : randomString(6);
    const person = `${participantName}__${postfix}`;
    // A group made for one student taken aside is theirs, and the staff's.
    if (groupRoom?.only && role === 'student' && !groupRoom.only.includes(person)) {
      return say('This group is not open to join.', 403);
    }
    // The role, and who this is, are signed into the token, so a browser cannot give
    // itself either. Every tab of one person carries the same `ch.person`, and the
    // lecturer and the people list count by it. (With real accounts, it would be the
    // account.)
    const attributes: Record<string, string> = { 'ch.role': role, 'ch.person': person };
    let identity = person;
    if (tabs === 'also') {
      // Another tab of someone here: a connection of its own, always, so that two
      // tabs moving into a group at the same moment cannot knock each other out.
      identity = `${person}__t${randomString(4)}`;
    } else if (tabs === 'switch') {
      // Here instead: any other tab of theirs is let go. (The one with the same
      // identity is dropped by LiveKit itself, when this one joins.)
      await letGoOf(roomName, person);
    } else if (await isHere(roomName, person)) {
      return NextResponse.json({ already: true }, { status: 409 });
    }
    const token = new AccessToken(API_KEY, API_SECRET, {
      identity,
      name: participantName,
      attributes,
    });
    token.ttl = '5m';
    const staff = role !== 'student';
    const grant: VideoGrant = {
      room: roomName,
      roomJoin: true,
      // A student cannot publish anything on joining. The lecturer grants the
      // microphone when it gives them the floor or opens the mics.
      canPublish: staff,
      canPublishSources: staff
        ? [TrackSource.MICROPHONE, TrackSource.SCREEN_SHARE, TrackSource.SCREEN_SHARE_AUDIO]
        : [],
      canPublishData: true,
      canSubscribe: true,
    };
    token.addGrant(grant);

    const data: ConnectionDetails = {
      serverUrl: LIVEKIT_URL,
      roomName,
      participantName,
      participantToken: await token.toJwt(),
    };
    return new NextResponse(JSON.stringify(data), {
      headers: {
        'Content-Type': 'application/json',
        'Set-Cookie': `${COOKIE_KEY}=${postfix}.${signed(postfix)}; Path=/; HttpOnly; SameSite=Strict; Max-Age=7200`,
      },
    });
  } catch (error) {
    // The details are in the server's log; the browser is told only that it failed.
    console.error(error);
    return say('Something went wrong joining the class. Try again in a moment.', 500);
  }
}

/**
 * The sender's address, or null if we cannot know it.
 *
 * `x-forwarded-for` is a list that each proxy adds to, and its left end is whatever
 * the sender wrote. Only the entries our own proxies added can be believed, so
 * CH_PROXY_HOPS says how many proxies of ours sit in front. With none, the header is
 * not used at all, and only the limit across all senders applies.
 */
function addressOf(request: NextRequest): string | null {
  const hops = Number(process.env.CH_PROXY_HOPS ?? 0);
  if (!(hops > 0)) return null;
  const list = (request.headers.get('x-forwarded-for') ?? '')
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean);
  return list[list.length - hops] ?? null;
}

/**
 * Counts in memory, so they are per server process: enough for one server, not for
 * several behind a load balancer. Joins: 60 from one address and 600 in all, in five
 * minutes (a whole class may share one campus address). Wrong staff codes: 5 in ten
 * minutes, so a code cannot be guessed by trying. Without CH_PROXY_HOPS every sender
 * shares one count, so on a real server it must be set.
 */
const joins = new Map<string, number[]>();
const wrongCodes = new Map<string, number[]>();
/** Whether `key` has done this fewer than `most` times in the last `window` ms, and
 *  (unless `add` is false) count this time. */
function notTooOften(
  counts: Map<string, number[]>,
  key: string,
  most: number,
  window: number,
  add = true,
) {
  const now = nowMs();
  const recent = (counts.get(key) ?? []).filter((at) => now - at < window);
  if (recent.length >= most) {
    counts.set(key, recent);
    return false;
  }
  if (add) recent.push(now);
  counts.set(key, recent);
  if (counts.size > 5000) counts.clear();
  return true;
}

/** 'student', or 'helper' / 'guest' if the code matches. null means a wrong code. */
function staffRole(as: string, code: string): 'student' | 'helper' | 'guest' | null {
  if (as !== 'helper' && as !== 'guest') return 'student';
  const wanted = as === 'helper' ? process.env.CH_HELPER_CODE : process.env.CH_GUEST_CODE;
  return wanted && code === wanted ? as : null;
}

/** A lesson that does not exist would leave the room waiting for a lecturer for ever. */
async function lessonExists(lesson: string): Promise<boolean> {
  return (await findLessonFile(`${lesson}.seg`)) !== null;
}

/**
 * Whether this room is a group of a class: a room that exists now and that a lecturer
 * marked as split from another (agent/src/lecturer/room/groups.py). Only names of the
 * shape groups are given are looked up.
 */
async function groupRoomOf(roomName: string): Promise<{ only?: string[] } | null> {
  if (!/-g\d-[a-f0-9]{4}$/.test(roomName)) return null;
  try {
    const rooms = new RoomServiceClient(LIVEKIT_URL!.replace(/^ws/, 'http'), API_KEY, API_SECRET);
    const [room] = await rooms.listRooms([roomName]);
    const meta = JSON.parse(room?.metadata || '{}');
    if (typeof meta.ch_group_of !== 'string') return null;
    return { only: Array.isArray(meta.only) ? meta.only.map(String) : undefined };
  } catch {
    return null;
  }
}

function roomService(): RoomServiceClient {
  return new RoomServiceClient(LIVEKIT_URL!.replace(/^ws/, 'http'), API_KEY, API_SECRET);
}

/** Whether this person is in the room now, in any tab. If this cannot be found out,
 *  they are taken not to be (and a tab of theirs with the same identity is dropped). */
async function isHere(roomName: string, person: string): Promise<boolean> {
  try {
    const people = await roomService().listParticipants(roomName);
    return people.some((p) => p.identity === person || p.attributes['ch.person'] === person);
  } catch {
    return false; // the room does not exist yet, or LiveKit could not be asked
  }
}

/** Disconnect this person's other tabs (not the one with their own identity). */
async function letGoOf(roomName: string, person: string): Promise<void> {
  try {
    const rooms = roomService();
    const people = await rooms.listParticipants(roomName);
    const extra = people.filter(
      (p) => p.attributes['ch.person'] === person && p.identity !== person,
    );
    await Promise.all(extra.map((p) => rooms.removeParticipant(roomName, p.identity)));
  } catch {
    // Nothing to let go of, or it could not be done: they stay in both.
  }
}

/** A mark that only this server can make for a cookie's value. */
function signed(value: string): string {
  return createHmac('sha256', API_SECRET!)
    .update(`ch-identity:${value}`)
    .digest('hex')
    .slice(0, 20);
}

/** Send the lecturer to the room, unless one has been sent already. */
async function ensureLecturer(roomName: string, metadata: LecturerJob) {
  const host = LIVEKIT_URL!.replace(/^ws/, 'http');
  const dispatch = new AgentDispatchClient(host, API_KEY, API_SECRET);
  // A room made by someone just joining has a nameless dispatch; ignore it.
  const sent = await dispatch.listDispatch(roomName).catch(() => []);
  if (!sent.some((d) => d.agentName === LECTURER)) {
    await dispatch.createDispatch(roomName, LECTURER, { metadata: JSON.stringify(metadata) });
  }
}

function randomString(length: number): string {
  const characters = 'abcdefghijklmnopqrstuvwxyz0123456789';
  let result = '';
  for (let i = 0; i < length; i++) {
    result += characters.charAt(Math.floor(Math.random() * characters.length));
  }
  return result;
}
