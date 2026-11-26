'use client';

import * as React from 'react';

const never = () => () => {};

/** A time shown in the reader's own time zone. Empty until the page has loaded. */
export function LocalTime({ at, withDay = true }: { at: number; withDay?: boolean }) {
  const text = React.useSyncExternalStore(
    never,
    () =>
      new Date(at).toLocaleString(undefined, {
        ...(withDay ? { weekday: 'long', day: 'numeric', month: 'short' } : {}),
        hour: 'numeric',
        minute: '2-digit',
      }),
    () => '',
  );
  return <time dateTime={new Date(at).toISOString()}>{text}</time>;
}
