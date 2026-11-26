'use client';

import * as React from 'react';

const never = () => () => {};

/**
 * A value this browser saved earlier, or null. It is read after the page has loaded,
 * in a way that keeps the first render the same on the server and in the browser.
 */
export function useSaved(key: string): string | null {
  return React.useSyncExternalStore(
    never,
    () => {
      try {
        return localStorage.getItem(key);
      } catch {
        return null;
      }
    },
    () => null,
  );
}

export function save(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {}
}
