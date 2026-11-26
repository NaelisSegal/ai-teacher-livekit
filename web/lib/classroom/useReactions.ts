'use client';

import { Room } from 'livekit-client';
import * as React from 'react';
import { readCapped } from './streams';

/**
 * Reactions: a thumbs up, a thumbs down, or "done", given in the moment. They are
 * not class state. One is sent to the room, shown by every browser for a few
 * seconds, and gone. The lecturer notices them too (agent/src/lecturer/room/react.py).
 */
export const REACT_TOPIC = 'ch.react';
export const REACTIONS = {
  up: { emoji: '👍', label: 'Thumbs up' },
  down: { emoji: '👎', label: 'Thumbs down' },
  done: { emoji: '✅', label: 'Done' },
} as const;
export type ReactKind = keyof typeof REACTIONS;

export type Reaction = { id: string; kind: ReactKind; name: string };

const SHOWN_MS = 4500;

export function useReactions(room: Room) {
  const [shown, setShown] = React.useState<Reaction[]>([]);
  const lastSent = React.useRef(0);

  const show = React.useCallback((reaction: Reaction) => {
    setShown((all) => [...all.slice(-11), reaction]);
    setTimeout(() => setShown((all) => all.filter((r) => r.id !== reaction.id)), SHOWN_MS);
  }, []);

  React.useEffect(() => {
    room.registerTextStreamHandler(REACT_TOPIC, async (reader, who) => {
      const kind = ((await readCapped(reader, 16)) ?? '').trim();
      if (!(kind in REACTIONS)) return;
      const name = room.remoteParticipants.get(who.identity)?.name || 'Someone';
      show({ id: reader.info.id, kind: kind as ReactKind, name });
    });
    return () => room.unregisterTextStreamHandler(REACT_TOPIC);
  }, [room, show]);

  const send = React.useCallback(
    (kind: ReactKind) => {
      const now = Date.now();
      if (now - lastSent.current < 800) return; // one at a time
      lastSent.current = now;
      show({ id: `mine-${now}`, kind, name: 'You' });
      room.localParticipant.sendText(kind, { topic: REACT_TOPIC }).catch(() => {});
    },
    [room, show],
  );

  return { shown, send };
}
