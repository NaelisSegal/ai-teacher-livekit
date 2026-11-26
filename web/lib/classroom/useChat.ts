'use client';

import { Room } from 'livekit-client';
import * as React from 'react';
import { readCapped } from './streams';

/** Class chat has its own topic. LiveKit's default chat topic would make the lecturer
 *  agent stop and answer every message. The other end is agent/src/lecturer/room/chat.py. */
export const CHAT_TOPIC = 'ch.chat';
export const CHAT_MAX_CHARS = 500;

export type ChatLine = { id: string; name: string; text: string; mine: boolean };

/** Chat for this class. Messages are kept only in this page, and only while it is open. */
export function useChat(room: Room) {
  const [lines, setLines] = React.useState<ChatLine[]>([]);

  React.useEffect(() => {
    let closed = false;
    room.registerTextStreamHandler(CHAT_TOPIC, async (reader, who) => {
      const whole = (await readCapped(reader, CHAT_MAX_CHARS * 4)) ?? '';
      const text = whole.trim().slice(0, CHAT_MAX_CHARS);
      if (closed || !text) return;
      const sender = room.remoteParticipants.get(who.identity);
      const name = sender?.name || who.identity;
      setLines((all) => [...all, { id: reader.info.id, name, text, mine: false }]);
    });
    return () => {
      closed = true;
      room.unregisterTextStreamHandler(CHAT_TOPIC);
    };
  }, [room]);

  const send = React.useCallback(
    async (raw: string) => {
      const text = raw.trim().slice(0, CHAT_MAX_CHARS);
      if (!text) return;
      const info = await room.localParticipant.sendText(text, { topic: CHAT_TOPIC });
      const name = room.localParticipant.name || 'You';
      setLines((all) => [...all, { id: info.id, name, text, mine: true }]);
    },
    [room],
  );

  return { lines, send };
}
