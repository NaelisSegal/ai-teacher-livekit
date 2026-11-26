'use client';

import { useLocalParticipant, useLocalParticipantPermissions } from '@livekit/components-react';
import { Room } from 'livekit-client';
import * as React from 'react';
import { askLecturer } from './lecturer';
import type { FloorState, MicMode } from './types';
import { REACTIONS, type ReactKind } from './useReactions';

export type Role = 'student' | 'helper' | 'guest';
export const ROLE_ATTR = 'ch.role';

export function roleOf(attributes: Readonly<Record<string, string>> | undefined): Role {
  const role = attributes?.[ROLE_ATTR];
  return role === 'helper' || role === 'guest' ? role : 'student';
}

const ask = askLecturer;

const MODE_NAMES: Record<MicMode, string> = {
  closed: 'Mics closed',
  hands: 'Raise hands',
  open: 'Mics open',
};

/**
 * Speaking controls. Unmute and Share screen are always there, and say why when
 * they cannot be used yet: a student is muted until the lecturer gives them the
 * floor or opens the mics. A helper or guest can use both at any time, and a helper
 * can run the room. Raising a hand and the reactions are a row of emoji.
 */
/** Thumbs up, thumbs down and done: answers without typing or speaking. */
function ReactButtons({ send }: { send: (kind: ReactKind) => void }) {
  return (
    <>
      {(Object.keys(REACTIONS) as ReactKind[]).map((kind) => (
        <button
          key={kind}
          className="ch-button ch-react"
          aria-label={REACTIONS[kind].label}
          title={REACTIONS[kind].label}
          onClick={() => send(kind)}
        >
          <span aria-hidden="true">{REACTIONS[kind].emoji}</span>
        </button>
      ))}
    </>
  );
}

export function FloorBar({
  room,
  floor,
  react,
}: {
  room: Room;
  floor: FloorState;
  react: (kind: ReactKind) => void;
}) {
  const { localParticipant, isMicrophoneEnabled, isScreenShareEnabled } = useLocalParticipant();
  const permissions = useLocalParticipantPermissions();
  const role = roleOf(localParticipant.attributes);
  const staff = role !== 'student';
  const me = localParticipant.identity;
  const mayPublish = !!permissions?.canPublish;
  const haveFloor = floor.speaker?.identity === me;
  const handUp = floor.hands.some((h) => h.identity === me);
  const [problem, setProblem] = React.useState('');
  // Class state, so it survives a reload and is the same for every member of staff.
  const paused = !!floor.paused;

  const setMic = React.useCallback(
    (on: boolean) => {
      setProblem('');
      localParticipant.setMicrophoneEnabled(on).catch(() => {
        setProblem(
          'The microphone could not be turned on. Check that this page is allowed to use it.',
        );
      });
    },
    [localParticipant],
  );

  // Given the floor: the microphone comes on (this is when the browser asks for it).
  React.useEffect(() => {
    if (haveFloor && mayPublish) {
      localParticipant.setMicrophoneEnabled(true).catch(() => {});
    }
  }, [haveFloor, mayPublish, localParticipant]);

  // The microphone was taken back: make sure ours is off.
  React.useEffect(() => {
    if (!mayPublish && localParticipant.isMicrophoneEnabled) {
      localParticipant.setMicrophoneEnabled(false).catch(() => {});
    }
  }, [mayPublish, localParticipant]);

  // Telling someone why a button did nothing, for a few seconds.
  const [told, setTold] = React.useState('');
  const tell = React.useCallback((text: string) => {
    setTold(text);
    setTimeout(() => setTold((now) => (now === text ? '' : now)), 5000);
  }, []);

  // The microphone and screen share buttons are always there. Someone who is not
  // allowed to use one yet is told why, instead of the button being hidden.
  const onMic = () => {
    if (isMicrophoneEnabled) return setMic(false);
    if (mayPublish) return setMic(true);
    if (floor.mode === 'hands') {
      tell(
        handUp
          ? 'Your hand is up. The lecturer will come to you.'
          : "Everyone is muted for now. Raise your hand ✋ if you'd like to speak.",
      );
    } else {
      tell('Everyone is muted for now.');
    }
  };
  const onShare = () => {
    if (!staff)
      return tell('Only the helper or a guest lecturer can share a screen in this class.');
    localParticipant
      .setScreenShareEnabled(!isScreenShareEnabled)
      .catch(() => setProblem('The screen could not be shared.'));
  };

  let note = '';
  if (haveFloor) note = 'You have the floor.';
  else if (floor.mode === 'open' && !staff) note = floor.prompt || 'The mics are open.';
  else if (handUp) {
    const ahead = floor.hands.findIndex((h) => h.identity === me);
    note =
      ahead <= 0 ? 'Your hand is up. You are next.' : `Your hand is up. ${ahead} ahead of you.`;
  } else if (floor.speaker && !staff) note = `${floor.speaker.name} has the floor.`;

  return (
    <div className="ch-floor">
      <button className="ch-button" aria-pressed={isMicrophoneEnabled} onClick={onMic}>
        {isMicrophoneEnabled ? 'Mute' : 'Unmute'}
      </button>
      <button className="ch-button" aria-pressed={isScreenShareEnabled} onClick={onShare}>
        {isScreenShareEnabled ? 'Stop sharing' : 'Share screen'}
      </button>
      {haveFloor ? (
        <button
          className="ch-button"
          onClick={() => {
            setMic(false);
            void ask(room, 'ch.done', '');
          }}
        >
          Finished speaking
        </button>
      ) : null}
      {/* Your own state, always in the same place. */}
      {isMicrophoneEnabled ? <span className="ch-floor-on">Your mic is on.</span> : null}
      {isScreenShareEnabled ? (
        <span className="ch-floor-on">You are sharing your screen.</span>
      ) : null}
      {note ? <span className="ch-floor-note">{note}</span> : null}

      <span className="ch-floor-sep" />
      {staff ? null : (
        <button
          className="ch-button ch-react"
          aria-pressed={handUp}
          aria-label={handUp ? 'Lower hand' : 'Raise hand'}
          title={handUp ? 'Lower hand' : 'Raise hand'}
          onClick={async () => {
            if (floor.mode !== 'hands' && !handUp) {
              return tell(
                floor.mode === 'open'
                  ? 'The mics are open. You can just unmute.'
                  : 'Hands are not being taken at the moment.',
              );
            }
            await ask(room, 'ch.hand', handUp ? 'down' : 'up');
          }}
        >
          <span aria-hidden="true">✋</span>
        </button>
      )}
      <ReactButtons send={react} />

      {role === 'helper' ? (
        <>
          <span className="ch-floor-sep" />
          {(['closed', 'hands', 'open'] as MicMode[]).map((mode) => (
            <button
              key={mode}
              className="ch-button"
              aria-pressed={floor.mode === mode}
              onClick={() => ask(room, 'ch.staff', JSON.stringify({ action: 'mode', mode }))}
            >
              {MODE_NAMES[mode]}
            </button>
          ))}
          {floor.speaker ? (
            <button
              className="ch-button"
              onClick={() => ask(room, 'ch.staff', JSON.stringify({ action: 'take' }))}
            >
              End {floor.speaker.name}&rsquo;s turn
            </button>
          ) : null}
          <button
            className="ch-button"
            aria-pressed={paused}
            onClick={() =>
              ask(room, 'ch.staff', JSON.stringify({ action: paused ? 'resume' : 'pause' }))
            }
          >
            {paused ? 'Let the lecturer go on' : 'Pause the lecturer'}
          </button>
        </>
      ) : null}
      {staff && floor.hands.length > 0 ? (
        <span className="ch-floor-note">Hands up: {floor.hands.map((h) => h.name).join(', ')}</span>
      ) : null}
      {told ? (
        <span className="ch-floor-told" role="status">
          {told}
        </span>
      ) : null}
      {problem ? <span className="ch-error">{problem}</span> : null}
    </div>
  );
}
