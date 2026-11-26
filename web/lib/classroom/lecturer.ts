import { ParticipantKind, Room } from 'livekit-client';

/** Ask the lecturer for something. Returns false if it could not be asked. */
export async function askLecturer(room: Room, method: string, payload: string): Promise<boolean> {
  const lecturer = [...room.remoteParticipants.values()].find(
    (p) => p.kind === ParticipantKind.AGENT,
  );
  if (!lecturer) return false;
  try {
    await room.localParticipant.performRpc({
      destinationIdentity: lecturer.identity,
      method,
      payload,
    });
    return true;
  } catch (e) {
    console.warn(`${method} failed`, e);
    return false;
  }
}
