// The browser applies the lecturer's events by the same rules as the lecturer does
// (agent/src/lecturer/room/board.py, stage.py, floor.py, polls.py). The recordings in
// agent/tests/shared/ are the events of short test lessons and the state the lecturer
// came to (checked by agent/tests/test_shared_state.py). Here, a browser given those
// events must come to the same state as a browser given that state as a snapshot.

import { readdirSync, readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { ClassStore } from './store';
import type { ClassEvent, Snapshot } from './types';

const folder = new URL('../../../agent/tests/shared/', import.meta.url);
const recordings = readdirSync(folder).filter((name) => name.endsWith('.json'));

type Recording = {
  start: Pick<Snapshot, 'board' | 'stage' | 'floor' | 'polls'>;
  events: ClassEvent[];
  state: Pick<Snapshot, 'board' | 'stage' | 'floor' | 'polls'>;
};

function snapshot(seq: number, state: Partial<Snapshot>): Snapshot {
  return {
    type: 'snapshot',
    seq,
    phase: 'teaching',
    plan: {},
    caption: null,
    pause: null,
    ...state,
  } as Snapshot;
}

/** The state both sides keep, without what only a browser has: how long a line that
 *  arrived live takes to be drawn, and when a video is at by this browser's clock. */
function shared(store: ClassStore) {
  const { board, stage, floor, polls } = store.state;
  const lines = board.lines.map(({ revealMs: _drawing, ...line }) => line);
  return {
    board: { ...board, lines },
    stage: { ...stage, video: { ...stage.video, at: 0 } },
    floor,
    polls,
  };
}

describe('the browser and the lecturer agree', () => {
  it('has recordings to check', () => {
    expect(recordings.length).toBeGreaterThan(0);
  });

  for (const name of recordings) {
    it(`comes to the lecturer's state for ${name}`, () => {
      const recording = JSON.parse(readFileSync(new URL(name, folder), 'utf8')) as Recording;
      const last = recording.events[recording.events.length - 1]?.seq ?? 0;

      const followed = new ClassStore();
      followed.receive(snapshot(0, recording.start), 0);
      followed.receive({ type: 'events', events: recording.events }, 0);

      const told = new ClassStore();
      told.receive(snapshot(last, recording.state), 0);

      expect(followed.lastSeq).toBe(last);
      expect(shared(followed)).toEqual(shared(told));
    });
  }
});
