'use client';

import {
  RoomAudioRenderer,
  StartAudio,
  useConnectionState,
  useRoomContext,
} from '@livekit/components-react';
import { ConnectionState } from 'livekit-client';
import * as React from 'react';
import { ChatPanel } from './ChatPanel';
import { FloorBar } from './FloorBar';
import { GroupsPanel } from './GroupsPanel';
import { PeopleButton, Speakers } from './People';
import { StageView } from './StageView';
import type { Caption, Pause } from './types';
import { useChat } from './useChat';
import { useClass } from './useClass';
import { REACTIONS, useReactions } from './useReactions';
import { save, useSaved } from './useSaved';
import { SoundContext } from './VideoView';
import { SITE_NAME } from '../site';

const VOLUME_KEY = 'ch.volume';

/**
 * The same class open in two tabs plays the lecturer twice, a little out of step,
 * which sounds like two voices. So only one tab plays sound: the newest, or the one
 * where the student asks for it.
 */
function useSoundInOneTab(roomName: string) {
  const [here, setHere] = React.useState(true);
  const channel = React.useRef<BroadcastChannel | null>(null);
  React.useEffect(() => {
    if (typeof BroadcastChannel === 'undefined' || !roomName) return;
    const c = new BroadcastChannel(`ch.sound.${roomName}`);
    channel.current = c;
    c.onmessage = (e) => {
      if (e.data === 'sound is here') setHere(false);
    };
    c.postMessage('sound is here');
    return () => {
      channel.current = null;
      c.close();
    };
  }, [roomName]);
  const take = React.useCallback(() => {
    channel.current?.postMessage('sound is here');
    setHere(true);
  }, []);
  return { here, take };
}

/** Shows its children once it has been on the page for `ms`. */
function AfterAWhile({ ms, children }: { ms: number; children: React.ReactNode }) {
  const [shown, setShown] = React.useState(false);
  React.useEffect(() => {
    const timer = setTimeout(() => setShown(true), ms);
    return () => clearTimeout(timer);
  }, [ms]);
  return shown ? <>{children}</> : null;
}

function CaptionBar({ caption }: { caption: Caption | null }) {
  // A finished sentence stays up for a few seconds, so short gaps do not blink.
  const [staleId, setStaleId] = React.useState<string | null>(null);
  const id = caption?.id;
  const done = caption?.done;
  React.useEffect(() => {
    if (!id || !done) return;
    const timer = setTimeout(() => setStaleId(id), 4000);
    return () => clearTimeout(timer);
  }, [id, done]);
  const stale = !!done && staleId === id;

  return (
    <div className="ch-caption" aria-live="polite">
      <p className={stale ? 'ch-caption-stale' : undefined}>{caption?.text ?? ''}</p>
    </div>
  );
}

function PauseBar({ pause }: { pause: Pause }) {
  // Worked out once, when the bar appears: it then runs down by itself.
  const [left] = React.useState(() => Math.max(0, pause.endsAt - Date.now()));
  return (
    <div className="ch-pause">
      <div className="ch-pause-text">
        {pause.watching ? null : <strong>Take a moment.</strong>} {pause.text}
      </div>
      <div className="ch-pause-track">
        <div className="ch-pause-left" style={{ animationDuration: `${left}ms` }} />
      </div>
    </div>
  );
}

export function Classroom({
  onLeave,
  onRestart,
  onMove,
  cameFrom,
}: {
  /** Goes to another room: a group, or back to the main class. */
  onMove: (roomName: string) => void;
  /** The main class's room, if this one is a group of it. */
  cameFrom?: string;
  onLeave: () => void;
  /** Starts the same lesson in a new room. Absent where rooms cannot be made on demand. */
  onRestart?: () => void;
}) {
  const room = useRoomContext();
  const cls = useClass(room);
  const connection = useConnectionState(room);
  const [chatOpen, setChatOpen] = React.useState(true);
  // A volume control of our own, for people whose computer's control is too coarse.
  const savedVolume = useSaved(VOLUME_KEY);
  const [chosenVolume, setChosenVolume] = React.useState<number | null>(null);
  const remembered =
    savedVolume !== null && /^\d{1,3}$/.test(savedVolume)
      ? Math.min(Number(savedVolume), 100)
      : 100;
  const volume = chosenVolume ?? remembered;
  const changeVolume = (value: number) => {
    setChosenVolume(value);
    save(VOLUME_KEY, String(value));
  };
  const sound = useSoundInOneTab(room.name);
  // Chat is followed here, not in the panel, so closing the panel loses nothing.
  const chat = useChat(room);
  const reactions = useReactions(room);

  // A room whose class is over, or whose lecturer never came, stays that way. Offer
  // a way to start again, since a class cannot be restarted in the same room.
  const over = cls.synced && cls.phase === 'ended';
  const waiting = connection === ConnectionState.Connected && !cls.lecturerHere && !over;

  let status = '';
  if (connection === ConnectionState.Reconnecting) status = 'Reconnecting…';
  else if (connection !== ConnectionState.Connected) status = 'Connecting…';
  else if (cls.synced && cls.phase === 'ended') status = 'The class has ended.';
  else if (!cls.lecturerHere) status = 'Waiting for the lecturer to arrive.';
  else if (cls.synced && cls.floor.paused) status = 'A helper has paused the lecturer.';
  else if (!cls.synced) status = 'Catching up…';
  else if (cls.phase === 'waiting') status = 'The class is about to start.';

  return (
    <div className="ch-room">
      <header className="ch-top">
        <div className="ch-top-title">
          <span className="ch-brand">{SITE_NAME}</span>
          {cls.plan.title ? <span className="ch-segment">{cls.plan.title}</span> : null}
        </div>
        <div className="ch-top-right">
          <label className="ch-volume">
            Volume
            <input
              id="volume"
              type="range"
              min={0}
              max={100}
              step={1}
              value={volume}
              onChange={(e) => changeVolume(Number(e.target.value))}
            />
            <span>{volume}%</span>
          </label>
          <PeopleButton floor={cls.floor} />
          <button
            className="ch-button"
            aria-pressed={chatOpen}
            onClick={() => setChatOpen(!chatOpen)}
          >
            {/* A poll is in the chat: say so when the chat is closed. */}
            {!chatOpen && cls.polls.some((p) => p.open) ? 'Chat: a poll is open' : 'Chat'}
          </button>
          <button className="ch-button" onClick={onLeave}>
            Leave
          </button>
        </div>
      </header>

      <div className="ch-middle">
        <div className="ch-stage">
          <main className="ch-main">
            <GroupsPanel
              room={room}
              split={cls.split}
              asks={cls.asks}
              group={cls.group}
              cameFrom={cameFrom}
              onMove={onMove}
            />
            {/* A video is as loud as the class, and silent in a tab that is not the
                one playing the class's sound. */}
            <SoundContext.Provider value={sound.here ? volume / 100 : 0}>
              <StageView board={cls.board} stage={cls.stage} />
            </SoundContext.Provider>
            {!onRestart ? null : over ? (
              <button className="ch-button ch-restart" onClick={onRestart}>
                Start this class again from the beginning
              </button>
            ) : waiting ? (
              <AfterAWhile ms={8000}>
                <button className="ch-button ch-restart" onClick={onRestart}>
                  No lecturer has come. Start a new class
                </button>
              </AfterAWhile>
            ) : null}
            {status ? <div className="ch-status">{status}</div> : null}
            {/* Reactions show for a few seconds, then go. */}
            <div className="ch-reactions" aria-live="polite">
              {reactions.shown.map((r) => (
                <div key={r.id} className="ch-reaction">
                  <span aria-hidden="true">{REACTIONS[r.kind].emoji}</span> {r.name}
                  <span className="ch-sr"> {REACTIONS[r.kind].label}</span>
                </div>
              ))}
            </div>
          </main>
          <footer className="ch-bottom">
            {cls.pause ? <PauseBar key={cls.pause.endsAt} pause={cls.pause} /> : null}
            <CaptionBar caption={cls.caption} />
            <FloorBar room={room} floor={cls.floor} react={reactions.send} />
          </footer>
        </div>
        <Speakers floor={cls.floor} />
        {chatOpen ? <ChatPanel room={room} chat={chat} polls={cls.polls} /> : null}
      </div>

      <RoomAudioRenderer volume={volume / 100} muted={!sound.here} />
      {sound.here ? null : (
        <button className="ch-button ch-start-audio" onClick={sound.take}>
          This class is open in another tab, and the sound is playing there. Play it here
        </button>
      )}
      <StartAudio label="Click to hear the lecturer" className="ch-button ch-start-audio" />
    </div>
  );
}
