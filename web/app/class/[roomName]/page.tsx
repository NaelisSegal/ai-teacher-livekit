import { findSession, KIND_NAMES, OPEN_ROOMS } from '@/lib/schedule';
import { ClassClient } from './ClassClient';

export default async function Page({
  params,
  searchParams,
}: {
  params: Promise<{ roomName: string }>;
  searchParams: Promise<{
    lesson?: string;
    as?: string;
    code?: string;
    auto?: string;
    from?: string;
  }>;
}) {
  const roomName = decodeURIComponent((await params).roomName);
  const { lesson, as, code, auto, from } = await searchParams;
  // If this room is on the schedule, the join screen says what it is.
  const session = await findSession(roomName);
  const info = session
    ? {
        kind: KIND_NAMES[session.kind],
        title: session.title,
        about: session.about,
        starts: session.starts,
        minutes: session.minutes,
      }
    : null;
  // The key gives each room its own page state, so going to a new room starts clean.
  return (
    <ClassClient
      key={roomName}
      roomName={roomName}
      lesson={lesson}
      as={as}
      code={code}
      info={info}
      canRestart={OPEN_ROOMS}
      auto={auto === '1'}
      cameFrom={from && /^[\w-]{1,64}$/.test(from) ? from : undefined}
      // Which services chat may be sent to depends on the models the lecturer is set
      // up with, so it is said here by whoever runs the site.
      aiServices={process.env.CH_AI_SERVICES ?? ''}
    />
  );
}
