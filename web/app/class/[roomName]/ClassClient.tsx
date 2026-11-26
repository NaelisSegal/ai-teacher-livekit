'use client';

// Started from livekit-examples/meet (Apache-2.0): app/rooms/[roomName]/PageClientImpl.tsx.
// A class is not a video call, so the camera and microphone pre-join is gone: students
// join with a name, listen, and watch the board.

import { RoomContext } from '@livekit/components-react';
import { DisconnectReason, Room, RoomEvent } from 'livekit-client';
import { useRouter } from 'next/navigation';
import * as React from 'react';
import { Classroom } from '@/lib/classroom/Classroom';
import { save, useSaved } from '@/lib/classroom/useSaved';
import { LocalTime } from '@/lib/LocalTime';
import type { ConnectionDetails } from '@/lib/types';

const NAME_KEY = 'ch.name';
// This tab is a second one of someone in the class ("Join here too"). Kept for the
// tab only, so that it stays one when it moves into a group and back.
const ALSO_KEY = 'ch.also';

function alsoTab(): boolean {
  try {
    return sessionStorage.getItem(ALSO_KEY) === '1';
  } catch {
    return false;
  }
}

function markAlso(on: boolean): void {
  try {
    if (on) sessionStorage.setItem(ALSO_KEY, '1');
    else sessionStorage.removeItem(ALSO_KEY);
  } catch {
    // Without storage the tab asks again after a move. That is all.
  }
}

export type SessionInfo = {
  kind: string;
  title: string;
  about: string;
  starts: number;
  minutes: number;
};

export function ClassClient(props: {
  roomName: string;
  lesson?: string;
  aiServices?: string;
  /** 'helper' or 'guest', with the code that allows it. Otherwise a student. */
  as?: string;
  code?: string;
  /** What this session is, if it is on the schedule. Shown under the join button. */
  info?: SessionInfo | null;
  /** Whether a new room can be made for another run (development only). */
  canRestart?: boolean;
  /** Join at once with the name already given: set when moving between a class and
   *  one of its groups. */
  auto?: boolean;
  /** The main class's room, if this room is a group of it. */
  cameFrom?: string;
}) {
  const router = useRouter();
  const savedName = useSaved(NAME_KEY);
  const [typedName, setName] = React.useState<string | null>(null);
  const name = typedName ?? savedName ?? '';
  const [joining, setJoining] = React.useState(false);
  const [error, setError] = React.useState('');
  const [room, setRoom] = React.useState<Room | undefined>(undefined);
  // Already in this class in another tab or window (asked on joining), or this tab was
  // replaced by another one.
  const [elsewhere, setElsewhere] = React.useState<'' | 'ask' | 'moved'>('');
  const leaving = React.useRef(false);

  const join = async (e?: React.FormEvent, tabs?: 'switch' | 'also') => {
    e?.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || joining) return;
    setJoining(true);
    setError('');
    setElsewhere('');
    leaving.current = false;
    if (tabs) markAlso(tabs === 'also');
    const how = tabs ?? (alsoTab() ? 'also' : undefined);
    try {
      save(NAME_KEY, trimmed);
      const resp = await fetch('/api/connection-details', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          roomName: props.roomName,
          participantName: trimmed,
          lesson: props.lesson,
          as: props.as,
          code: props.code,
          tabs: how,
        }),
      });
      if (resp.status === 409) {
        setElsewhere('ask');
        return;
      }
      if (!resp.ok) throw new Error(await resp.text());
      const details = (await resp.json()) as ConnectionDetails;

      const next = new Room({ adaptiveStream: true });
      next.on(RoomEvent.Disconnected, (reason?: DisconnectReason) => {
        setRoom(undefined);
        if (leaving.current) return;
        // Replaced by a tab with the same identity, or let go of when the person chose
        // "Switch here" in another tab. (Nothing else removes a student from a class.)
        if (
          reason === DisconnectReason.DUPLICATE_IDENTITY ||
          reason === DisconnectReason.PARTICIPANT_REMOVED
        ) {
          setElsewhere('moved');
        } else setError('You were disconnected from the class. You can join again.');
      });
      // Joining is a click, so the browser lets us play sound from here on.
      void next.startAudio().catch(() => {});
      await next.connect(details.serverUrl, details.participantToken);
      setRoom(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not join.');
    } finally {
      setJoining(false);
    }
  };

  const leave = React.useCallback(() => {
    leaving.current = true;
    void room?.disconnect();
    router.push('/');
  }, [room, router]);

  // A new room with the same lesson. The class starts from the beginning there.
  const restart = React.useCallback(() => {
    leaving.current = true;
    void room?.disconnect();
    const base = props.roomName.replace(/-[a-z0-9]{4}$/, '');
    const fresh = Math.random().toString(36).slice(2, 6);
    const query = props.lesson ? `?lesson=${encodeURIComponent(props.lesson)}` : '';
    router.push(`/class/${base}-${fresh}${query}`);
  }, [room, router, props.roomName, props.lesson]);

  // To a group of this class, or back from one. The page for that room joins by itself.
  const move = React.useCallback(
    (target: string) => {
      if (!/^[\w-]{1,64}$/.test(target)) return;
      leaving.current = true;
      void room?.disconnect();
      const query = new URLSearchParams({ auto: '1' });
      // Carried along, so that it is still there on the way back. (Only a class that
      // is not on the schedule is joined with a lesson: a development class.)
      if (props.lesson) query.set('lesson', props.lesson);
      // Going into a group, the way back is taken along: the group's room may turn
      // out to have no lecturer to say where it came from.
      if (/-g\d-[a-f0-9]{4}$/.test(target)) query.set('from', props.cameFrom ?? props.roomName);
      if (props.as && props.code) {
        query.set('as', props.as);
        query.set('code', props.code);
      }
      router.push(`/class/${target}?${query}`);
    },
    [room, router, props.lesson, props.as, props.code, props.cameFrom, props.roomName],
  );

  const arrived = React.useRef(false);
  React.useEffect(() => {
    if (props.auto && savedName && !arrived.current) {
      arrived.current = true;
      void join();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.auto, savedName]);

  React.useEffect(() => () => void room?.disconnect(), [room]);

  if (room) {
    return (
      <RoomContext.Provider value={room}>
        <Classroom
          onLeave={leave}
          onMove={move}
          cameFrom={props.cameFrom}
          onRestart={props.canRestart ? restart : undefined}
        />
      </RoomContext.Provider>
    );
  }

  return (
    <main className="ch-join">
      <form onSubmit={join}>
        <h1>{props.info ? props.info.title : 'Join the class'}</h1>
        {props.as === 'helper' || props.as === 'guest' ? (
          <p>
            You are joining as {props.as === 'helper' ? 'the helper' : 'a guest lecturer'}. You can
            unmute and share your screen at any time.
          </p>
        ) : (
          <p>
            You will hear the lecturer and see the board. Your microphone stays off unless you are
            given the floor or the mics are opened. What you type in the chat, and what you say when
            your microphone is on, may be sent to an AI service
            {props.aiServices ? ` (${props.aiServices.split(',').join(', ')})` : ''}
            {' so the lecturer can answer it.'}
          </p>
        )}
        <label htmlFor="name">Your name</label>
        <input
          id="name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          maxLength={30}
          autoComplete="nickname"
          autoFocus
        />
        {elsewhere ? (
          <div className="ch-elsewhere" role="status">
            <p>
              {elsewhere === 'moved'
                ? 'This class is now open in another tab or window.'
                : 'You are already in this class in another tab or window (or were, a moment ago).'}
            </p>
            <button
              type="button"
              className="ch-button ch-button-main"
              disabled={joining}
              onClick={() => void join(undefined, 'switch')}
            >
              {elsewhere === 'moved' ? 'Use this tab instead' : 'Switch here'}
            </button>
            {elsewhere === 'ask' ? (
              <button
                type="button"
                className="ch-button"
                disabled={joining}
                onClick={() => void join(undefined, 'also')}
              >
                Join here too
              </button>
            ) : null}
          </div>
        ) : (
          <button className="ch-button ch-button-main" disabled={!name.trim() || joining}>
            {joining ? 'Joining…' : 'Join'}
          </button>
        )}
        {error ? <p className="ch-error">{error}</p> : null}
      </form>
      {props.info ? (
        // Enough to know what this is, without leaving the page.
        <section className="ch-join-info" aria-label="About this session">
          <p>
            <strong>{props.info.kind}.</strong> Starts at{' '}
            <LocalTime at={props.info.starts} withDay={false} />, about {props.info.minutes}{' '}
            minutes.
          </p>
          {props.info.about ? <p>{props.info.about}</p> : null}
        </section>
      ) : null}
    </main>
  );
}
