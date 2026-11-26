'use client';

import { ParticipantKind, Room, RoomEvent, RpcError } from 'livekit-client';
import * as React from 'react';
import { ClassStore, emptyState } from './store';
import { readCapped } from './streams';
import { CLASS_TOPIC, ClassMessage, ClassState, SYNC_RPC } from './types';

/** How long a missing event may be late before we ask for a snapshot. */
const GAP_WAIT_MS = 700;

function lecturerIdentity(room: Room): string | undefined {
  for (const p of room.remoteParticipants.values()) {
    if (p.kind === ParticipantKind.AGENT) return p.identity;
  }
  return undefined;
}

/** Follows the class in `room`: the board, captions, pauses and plan position. */
export function useClass(room: Room): ClassState & { lecturerHere: boolean } {
  const [state, setState] = React.useState<ClassState>(emptyState);
  const [lecturerHere, setLecturerHere] = React.useState(false);

  React.useEffect(() => {
    const store = new ClassStore();
    let closed = false;
    let syncing = false;
    let gapTimer: ReturnType<typeof setTimeout> | undefined;

    const sync = async () => {
      if (syncing || closed) return;
      syncing = true;
      try {
        // Keep trying for as long as we are out of step. The waits grow to 5 seconds.
        for (let attempt = 0; !closed && !store.state.synced; attempt++) {
          const lecturer = lecturerIdentity(room);
          if (lecturer) {
            try {
              await room.localParticipant.performRpc({
                destinationIdentity: lecturer,
                method: SYNC_RPC,
                payload: '',
              });
              // The snapshot follows on the class topic. Give it a moment.
              await new Promise((r) => setTimeout(r, 1500));
              continue;
            } catch (e) {
              if (!(e instanceof RpcError)) console.warn('class sync failed', e);
            }
          }
          await new Promise((r) => setTimeout(r, Math.min(400 + attempt * 300, 5000)));
        }
      } finally {
        syncing = false;
      }
    };

    const resync = () => {
      store.lose();
      setState(store.state);
      void sync();
    };

    const onPeople = () => {
      const here = lecturerIdentity(room) !== undefined;
      setLecturerHere(here);
      if (here && !store.state.synced) void sync();
    };

    room.registerTextStreamHandler(CLASS_TOPIC, async (reader, sender) => {
      // Only the lecturer may speak on this topic. Students can send data too, so
      // without this check any student could draw on everyone's board.
      const from = room.remoteParticipants.get(sender.identity);
      if (from?.kind !== ParticipantKind.AGENT) {
        void readCapped(reader, 0); // drop it, so it is not left buffering
        return;
      }
      const text = await readCapped(reader, 8_000_000);
      if (closed || text === null) return;
      try {
        store.receive(JSON.parse(text) as ClassMessage);
      } catch (e) {
        console.warn('bad class message', e);
        return;
      }
      setState(store.state);
      if (store.needsSnapshot()) void sync();
      if (store.hasGap()) {
        gapTimer ??= setTimeout(() => {
          gapTimer = undefined;
          if (store.hasGap()) resync();
        }, GAP_WAIT_MS);
      }
    });

    room
      .on(RoomEvent.Connected, onPeople)
      .on(RoomEvent.ParticipantConnected, onPeople)
      .on(RoomEvent.ParticipantDisconnected, onPeople)
      .on(RoomEvent.Reconnected, resync);
    onPeople();

    return () => {
      closed = true;
      clearTimeout(gapTimer);
      room.unregisterTextStreamHandler(CLASS_TOPIC);
      room
        .off(RoomEvent.Connected, onPeople)
        .off(RoomEvent.ParticipantConnected, onPeople)
        .off(RoomEvent.ParticipantDisconnected, onPeople)
        .off(RoomEvent.Reconnected, resync);
    };
  }, [room]);

  return { ...state, lecturerHere };
}
