'use client';

import { useLocalParticipant, useRemoteParticipants } from '@livekit/components-react';
import { ParticipantKind, Room } from 'livekit-client';
import * as React from 'react';
import { roleOf } from './FloorBar';
import { askLecturer } from './lecturer';
import type { Ask, GroupHere, Split } from './types';

const SPLIT_RPC = 'ch.split';
const NO_LECTURER_MS = 30_000;

// Whether this tab has already been sent to a group's room. Storage can be blocked:
// then the tab only forgets, it does not break.
function wasSent(room: string): boolean {
  try {
    return sessionStorage.getItem(`ch.sent.${room}`) !== null;
  } catch {
    return false;
  }
}

function markSent(room: string): void {
  try {
    sessionStorage.setItem(`ch.sent.${room}`, '1');
  } catch {
    // Nothing to keep it in.
  }
}

/**
 * Groups. The class can split, each group in a room of its own with its own lecturer.
 * The lecturer cannot move anyone: each browser takes its own student to their group,
 * and back to the main class at the end.
 *
 * `onMove` goes to another room. It is called once for each place a student is sent.
 */
export function GroupsPanel({
  room,
  split,
  asks,
  group,
  cameFrom,
  onMove,
}: {
  /** The room this browser came here from: the main class, if this is a group. Kept
   *  by the browser, so that there is a way back even if this room has no lecturer. */
  cameFrom?: string;
  room: Room;
  split: Split | null;
  asks: Ask[];
  group: GroupHere | null;
  onMove: (roomName: string) => void;
}) {
  const { localParticipant } = useLocalParticipant();
  const others = useRemoteParticipants();
  const staff = roleOf(localParticipant.attributes) !== 'student';
  const me = localParticipant.identity;
  const mine = split?.to[me];
  const sent = React.useRef('');

  // Sent back from a group: go, once.
  React.useEffect(() => {
    if (group?.back && sent.current !== 'back') {
      sent.current = 'back';
      onMove(group.main);
    }
  }, [group?.back, group?.main, onMove]);

  // Put in a group (by chance, by a helper, or taken aside): go there, once. Someone
  // who has come back by choice is not sent again; they are shown the way instead.
  // It depends on the room's name alone, so that other students being placed (which
  // changes `split`) does not put off this student's own move.
  const myRoom = staff ? undefined : split?.groups.find((g) => g.id === mine && !g.ended)?.room;
  const move = React.useRef(onMove);
  React.useEffect(() => {
    move.current = onMove;
  }, [onMove]);
  React.useEffect(() => {
    if (!myRoom || sent.current === myRoom) return;
    if (wasSent(myRoom)) {
      sent.current = myRoom;
      return;
    }
    const timer = setTimeout(() => {
      sent.current = myRoom;
      markSent(myRoom);
      move.current(myRoom);
    }, 1500);
    return () => clearTimeout(timer);
  }, [myRoom]);

  const tell = (request: Record<string, unknown>) =>
    void askLecturer(room, SPLIT_RPC, JSON.stringify(request));

  const main = group?.main ?? cameFrom;
  // A group whose lecturer never comes (none was free to come, say) is no place to
  // wait: after a while, go back to the main class on our own.
  const lecturerHere = others.some((p) => p.kind === ParticipantKind.AGENT);
  React.useEffect(() => {
    if (!main || staff || lecturerHere) return;
    const timer = setTimeout(() => move.current(main), NO_LECTURER_MS);
    return () => clearTimeout(timer);
  }, [main, staff, lecturerHere]);
  if (main) {
    return (
      <div className="ch-groups ch-groups-here">
        <span>
          {group ? (
            <>
              You are in the group <strong>{group.title}</strong>.
            </>
          ) : (
            'You are in a group.'
          )}
        </span>
        <button className="ch-button" onClick={() => onMove(main)}>
          Back to the main class
        </button>
      </div>
    );
  }

  const waiting = staff ? asks : [];
  if (!split && waiting.length === 0) return null;
  const open = split?.groups.filter((g) => !g.ended) ?? [];
  const students = others.filter(
    (p) => p.kind !== ParticipantKind.AGENT && roleOf(p.attributes) === 'student',
  );
  const myGroup = open.find((g) => g.id === mine);

  return (
    <div className="ch-groups">
      {waiting.map((ask) => (
        <div key={ask.id} className="ch-groups-ask">
          <p>
            The lecturer would like to take <strong>{ask.name || 'a student'}</strong> aside for a
            few minutes. {ask.title}: {ask.why}
          </p>
          <button
            className="ch-button"
            onClick={() => tell({ action: 'answer', id: ask.id, ok: true })}
          >
            Agree
          </button>
          <button
            className="ch-button"
            onClick={() => tell({ action: 'answer', id: ask.id, ok: false })}
          >
            Not now
          </button>
        </div>
      ))}
      {split && split.how !== 'one' ? (
        <>
          <h2>
            The class is in groups{split.minutes ? `, for about ${split.minutes} minutes` : ''}.
          </h2>
          {myGroup ? (
            <p>
              You are in <strong>{myGroup.title}</strong>.
            </p>
          ) : split.how === 'choose' && !staff ? (
            <p>Pick the one that suits you. You can come back and pick another.</p>
          ) : !staff ? (
            <p>A helper is putting everyone in a group. You will be taken to yours.</p>
          ) : null}
          <ul>
            {open.map((g) => (
              <li key={g.id}>
                <div>
                  <strong>{g.title}</strong>
                  {g.about ? <span> {g.about}</span> : null}
                </div>
                {staff || split.how === 'choose' || g.id === mine ? (
                  <button className="ch-button ch-button-main" onClick={() => onMove(g.room)}>
                    {staff ? 'Go in' : g.id === mine ? 'Go to my group' : 'Join this group'}
                  </button>
                ) : null}
                {staff ? (
                  <button
                    className="ch-button"
                    onClick={() => tell({ action: 'close', title: g.title })}
                  >
                    End this group
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
          {staff ? (
            <>
              {students.length > 0 ? <h3>Students still here</h3> : null}
              {students.map((p) => (
                <div key={p.identity} className="ch-groups-student">
                  <span>{p.name || 'A student'}</span>
                  {open.map((g) => (
                    <button
                      key={g.id}
                      className="ch-button"
                      aria-pressed={split.to[p.identity] === g.id}
                      onClick={() => tell({ action: 'assign', identity: p.identity, group: g.id })}
                    >
                      {g.title}
                    </button>
                  ))}
                </div>
              ))}
              <button className="ch-button" onClick={() => tell({ action: 'close' })}>
                Bring everyone back
              </button>
            </>
          ) : null}
        </>
      ) : null}
      {split && split.how === 'one' && staff ? (
        <p>
          One student has been taken aside: <strong>{open[0]?.title}</strong>.{' '}
          {open[0] ? (
            <>
              <button className="ch-button" onClick={() => onMove(open[0].room)}>
                Go in
              </button>{' '}
              <button className="ch-button" onClick={() => tell({ action: 'close' })}>
                Bring them back
              </button>
            </>
          ) : null}
        </p>
      ) : null}
      {split && split.how === 'one' && myGroup ? (
        <p>
          The lecturer is taking you aside for a few minutes: <strong>{myGroup.title}</strong>.{' '}
          <button className="ch-button ch-button-main" onClick={() => onMove(myGroup.room)}>
            Go there
          </button>
        </p>
      ) : null}
    </div>
  );
}
