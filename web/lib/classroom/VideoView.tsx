'use client';

import * as React from 'react';
import type { VideoSrc, VideoState } from './types';

/** How loud the class is for this person, from 0 to 1 (their volume control). */
export const SoundContext = React.createContext(1);

/** Turned down this far while the lecturer talks over a video. */
const QUIET = 0.15;

function same(a: VideoSrc | null, b: VideoSrc | null): boolean {
  return !!a && !!b && a.kind === b.kind && a.id === b.id && a.form === b.form;
}

/** Where the video should be now, in seconds. */
function momentNow(video: VideoState): number {
  return video.position + (video.playing ? Math.max(0, Date.now() - video.at) / 1000 : 0);
}

/** A file from lesson prep, played by this browser and kept in step with the lecturer. */
function FileVideo({ src, video }: { src: VideoSrc; video: VideoState }) {
  const el = React.useRef<HTMLVideoElement>(null);
  const loud = React.useContext(SoundContext);
  // A browser may refuse to start a video with sound by itself. Then we ask.
  const [blocked, setBlocked] = React.useState(false);

  React.useEffect(() => {
    const player = el.current;
    if (!player) return;
    const moment = momentNow(video);
    // The lecturer does not know how long a video is. Once it is over it stays over:
    // asking a finished video to play would start it again from the beginning.
    if (video.playing && player.duration && moment >= player.duration) {
      player.pause();
      return;
    }
    if (Math.abs(player.currentTime - moment) > 0.4) player.currentTime = moment;
    if (video.playing) {
      player.play().then(
        () => setBlocked(false),
        () => setBlocked(true),
      );
    } else {
      player.pause();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [video.playing, video.position, video.at]);

  React.useEffect(() => {
    if (el.current) el.current.volume = Math.min(1, loud * (video.quiet ? QUIET : 1));
  }, [loud, video.quiet]);

  // A file's id is <folder>/<file>, in plain names. Anything else is not fetched.
  if (!/^[a-z0-9-]+\/\w[\w.-]*$/.test(src.id)) return null;
  return (
    <>
      <video ref={el} src={`/lesson-files/${src.id}`} playsInline preload="auto" />
      {blocked && video.playing ? (
        <button
          className="ch-button ch-video-start"
          onClick={() => {
            const player = el.current;
            if (!player) return;
            player.currentTime = momentNow(video);
            void player.play().then(() => setBlocked(false));
          }}
        >
          Play the video
        </button>
      ) : null}
    </>
  );
}

function twitchTime(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 3600)}h${Math.floor((s % 3600) / 60)}m${s % 60}s`;
}

/** The address of the site's own player, built here from what the site calls the
 *  video. Nothing a student typed is used as an address. */
function playerAddress(src: VideoSrc, video: VideoState): string | null {
  const start = Math.floor(momentNow(video));
  const host = window.location.hostname;
  if (src.kind === 'youtube' && /^[\w-]{11}$/.test(src.id)) {
    // The address YouTube gives for showing a video without it shaping what the
    // viewer is later shown on YouTube.
    const q = new URLSearchParams({
      autoplay: video.playing ? '1' : '0',
      start: String(start),
      playsinline: '1',
      rel: '0',
      // So that the page can tell the player to turn its subtitles off (below).
      enablejsapi: '1',
      origin: window.location.origin,
    });
    return `https://www.youtube-nocookie.com/embed/${src.id}?${q}`;
  }
  if (src.kind === 'twitch' && /^[\w-]{1,80}$/.test(src.id)) {
    const autoplay = video.playing ? 'true' : 'false';
    if (src.form === 'clip') {
      return `https://clips.twitch.tv/embed?clip=${src.id}&parent=${host}&autoplay=${autoplay}`;
    }
    const what = src.form === 'channel' ? `channel=${src.id}` : `video=v${src.id}`;
    const time = src.form === 'video' ? `&time=${twitchTime(start)}` : '';
    return `https://player.twitch.tv/?${what}&parent=${host}&autoplay=${autoplay}${time}`;
  }
  return null;
}

/**
 * A video on YouTube or Twitch, in the site's own player. The player is loaded
 * afresh, at the right moment, each time the lecturer plays, stops or moves the video:
 * that needs nothing from the site but its player, and no script of theirs in our page.
 * Its sound is the player's own, so the person watching sets it there.
 */
function SiteVideo({ src, video }: { src: VideoSrc; video: VideoState }) {
  const address = React.useMemo(
    () => playerAddress(src, video),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [src.kind, src.id, src.form, video.playing, video.position, video.at],
  );
  const frame = React.useRef<HTMLIFrameElement>(null);
  // Subtitles start off, as most people expect: some videos turn them on by
  // themselves. The player takes a moment to be ready, so it is told a few times.
  // (A student can still turn them on in the player.)
  const noSubtitles = React.useCallback(() => {
    if (src.kind !== 'youtube') return;
    for (const ms of [500, 1500, 3000, 6000]) {
      setTimeout(() => {
        frame.current?.contentWindow?.postMessage(
          JSON.stringify({ event: 'command', func: 'unloadModule', args: ['captions'] }),
          'https://www.youtube-nocookie.com',
        );
      }, ms);
    }
  }, [src.kind]);
  if (!address) return <div className="ch-pane-empty">This video cannot be shown.</div>;
  return (
    <iframe
      ref={frame}
      key={address}
      src={address}
      title="Video"
      onLoad={noSubtitles}
      allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
      referrerPolicy="strict-origin-when-cross-origin"
    />
  );
}

/**
 * One place a video is seen: the video pane of the stage, or a slide that holds a
 * video. There is one video at a time. A place whose video is not that one shows its
 * own, still, from the start.
 */
export function VideoView({ src, video }: { src: VideoSrc; video: VideoState }) {
  const state: VideoState = same(src, video.src)
    ? video
    : { src, playing: false, position: 0, quiet: false, at: 0 };
  return (
    <div className="ch-video" aria-label="Video">
      {src.kind === 'file' ? (
        <FileVideo src={src} video={state} />
      ) : (
        <SiteVideo src={src} video={state} />
      )}
    </div>
  );
}

/** The video pane of the stage. */
export function VideoPane({ video }: { video: VideoState }) {
  if (!video.src) return <div className="ch-pane-empty">No video is loaded yet.</div>;
  return <VideoView src={video.src} video={video} />;
}
