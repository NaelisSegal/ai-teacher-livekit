'use client';

import { useIsSpeaking, useParticipants, useSpeakingParticipants } from '@livekit/components-react';
import { Participant, ParticipantKind } from 'livekit-client';
import * as React from 'react';
import { roleOf } from './FloorBar';
import type { FloorState } from './types';

const ROLE_NAMES = { student: '', helper: 'helper', guest: 'guest lecturer' } as const;

/** Who is here. Kept behind a button: the class page does not show it otherwise. */
/** Who a participant is: every tab of one person carries the same `ch.person`. */
export function personOf(p: Participant): string {
  return p.attributes['ch.person'] || p.identity;
}

export function PeopleButton({ floor }: { floor: FloorState }) {
  const [open, setOpen] = React.useState(false);
  // A person here in two tabs is one person, shown once, with what any tab is doing.
  const tabs = useParticipants().filter((p) => p.kind !== ParticipantKind.AGENT);
  const byPerson = new Map<string, Participant[]>();
  for (const p of tabs) byPerson.set(personOf(p), [...(byPerson.get(personOf(p)) ?? []), p]);
  const people = [...byPerson.values()];
  return (
    <div className="ch-people">
      <button className="ch-button" aria-expanded={open} onClick={() => setOpen(!open)}>
        People
      </button>
      {open ? (
        <div className="ch-people-list" role="dialog" aria-label="People in this class">
          <p className="ch-people-count">
            {people.length === 1 ? '1 person here' : `${people.length} people here`}, and the
            lecturer
          </p>
          <ul>
            {people.map((all) => {
              const p = all[0];
              const ids = new Set(all.map((t) => t.identity));
              const role = ROLE_NAMES[roleOf(p.attributes)];
              const hand = floor.hands.some((h) => ids.has(h.identity));
              const has = !!floor.speaker && ids.has(floor.speaker.identity);
              const me = all.some((t) => t.isLocal);
              return (
                <li key={personOf(p)}>
                  <span>{p.name || 'Someone'}</span>
                  <span className="ch-people-tag">
                    {[me ? 'you' : '', role, has ? 'has the floor' : hand ? 'hand up' : '']
                      .filter(Boolean)
                      .join(', ')}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function SpeakerCard({ person, floor }: { person: Participant; floor: boolean }) {
  const speaking = useIsSpeaking(person);
  const role = ROLE_NAMES[roleOf(person.attributes)];
  return (
    <div className="ch-speaker" data-speaking={speaking}>
      <div className="ch-speaker-name">{person.name || 'Someone'}</div>
      <div className="ch-speaker-tag">
        {[person.isLocal ? 'you' : '', role, floor ? 'has the floor' : '']
          .filter(Boolean)
          .join(', ')}
      </div>
      {/* Bars that move while they talk. Movement, not light. */}
      <div className="ch-speaker-bars" aria-hidden="true">
        <i />
        <i />
        <i />
        <i />
        <i />
      </div>
    </div>
  );
}

/**
 * The people who are talking, and the student who has the floor. Nobody else is
 * shown: a person appears here only while they speak, and for a moment after.
 */
export function Speakers({ floor }: { floor: FloorState }) {
  const everyone = useParticipants();
  const speaking = useSpeakingParticipants().filter((p) => p.kind !== ParticipantKind.AGENT);
  // Keep a card up for a moment after its person stops, so it does not flicker.
  const [recent, setRecent] = React.useState<Record<string, number>>({});
  const ids = speaking.map((p) => p.identity).join('|');
  React.useEffect(() => {
    const timer = setTimeout(() => {
      const now = Date.now();
      setRecent((old) => {
        const next: Record<string, number> = {};
        for (const [id, at] of Object.entries(old)) if (now - at < 2500) next[id] = at;
        for (const id of ids ? ids.split('|') : []) next[id] = now;
        return next;
      });
    }, 0);
    const sweep = setInterval(() => {
      const now = Date.now();
      setRecent((old) =>
        Object.values(old).some((at) => now - at >= 2500)
          ? Object.fromEntries(Object.entries(old).filter(([, at]) => now - at < 2500))
          : old,
      );
    }, 1000);
    return () => {
      clearTimeout(timer);
      clearInterval(sweep);
    };
  }, [ids]);

  const shown = everyone.filter(
    (p) =>
      p.kind !== ParticipantKind.AGENT &&
      (p.identity === floor.speaker?.identity ||
        speaking.some((s) => s.identity === p.identity) ||
        p.identity in recent),
  );
  if (shown.length === 0) return null;
  return (
    <aside className="ch-speakers" aria-label="Speaking now">
      {shown.map((p) => (
        <SpeakerCard key={p.identity} person={p} floor={p.identity === floor.speaker?.identity} />
      ))}
    </aside>
  );
}
