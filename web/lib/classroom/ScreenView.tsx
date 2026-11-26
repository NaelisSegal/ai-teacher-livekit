'use client';

import { useTracks, VideoTrack } from '@livekit/components-react';
import { ParticipantKind, Track } from 'livekit-client';
import * as React from 'react';
import type { ScreenState } from './types';

function shortAddress(url: string): string {
  try {
    const u = new URL(url);
    return u.host + (u.pathname === '/' ? '' : u.pathname);
  } catch {
    return url;
  }
}

/**
 * The lecturer's shared screen: a real browser on the lecturer's side. The page
 * comes as video. The tab strip and the address are drawn here from what the
 * lecturer reports, so they stay sharp. Students watch; only the lecturer steers.
 */
export function ScreenView({ screen }: { screen: ScreenState }) {
  // Only the lecturer's screen is ever shown here.
  const track = useTracks([Track.Source.ScreenShare]).find(
    (t) => t.participant.kind === ParticipantKind.AGENT,
  );
  const current = screen.tabs[screen.active];
  return (
    <div className="ch-screen" aria-label="Shared screen">
      <div className="ch-browser-top">
        <div className="ch-tabs">
          {screen.tabs.map((tab, i) => (
            <div
              key={i}
              className="ch-tab"
              aria-current={i === screen.active ? 'true' : undefined}
              title={tab.title}
            >
              {tab.title || shortAddress(tab.url)}
            </div>
          ))}
          {screen.tabs.length === 0 ? <div className="ch-tab">No tab open</div> : null}
        </div>
        <div className="ch-address">{current ? shortAddress(current.url) : ''}</div>
      </div>
      {track && !screen.failed ? (
        <VideoTrack trackRef={track} className="ch-screen-video" />
      ) : (
        <div className="ch-pane-empty">
          {screen.failed ? 'The shared screen is not working.' : 'The shared screen is starting.'}
        </div>
      )}
    </div>
  );
}

/** A person's own shared screen: a guest lecturer's slides, or the helper's. */
export function ShareView({ who, name }: { who?: string; name?: string }) {
  const track = useTracks([Track.Source.ScreenShare]).find((t) => t.participant.identity === who);
  return (
    <div className="ch-screen" aria-label="Shared screen">
      <div className="ch-browser-top">
        <div className="ch-address">{name || 'Someone'} is sharing their screen</div>
      </div>
      {track ? (
        <VideoTrack trackRef={track} className="ch-screen-video" />
      ) : (
        <div className="ch-pane-empty">Waiting for the shared screen.</div>
      )}
    </div>
  );
}
