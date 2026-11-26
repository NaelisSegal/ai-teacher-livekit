import { describe, expect, it } from 'vitest';
import { boardRows, ClassStore } from './store';
import type { ClassEvent, Deck, Snapshot } from './types';

const snap = (seq: number, lines: string[] = []): Snapshot => ({
  type: 'snapshot',
  seq,
  phase: 'teaching',
  plan: {},
  board: {
    page: 1,
    top: 1,
    lines: lines.map((id) => ({ id, kind: 'math', content: id, marks: {} })),
    saved: {},
  },
  caption: null,
  pause: null,
});
const write = (seq: number, id: string): ClassEvent => ({
  seq,
  t: 0,
  type: 'board.write',
  id,
  kind: 'math',
  content: id,
});
const ids = (s: ClassStore) => s.state.board.lines.map((ln) => ln.id);

describe('ClassStore', () => {
  it('holds events until a snapshot arrives, then applies only the newer ones', () => {
    const s = new ClassStore();
    s.receive({ type: 'events', events: [write(4, 'old'), write(6, 'b')] });
    expect(s.state.synced).toBe(false);
    s.receive(snap(4, ['a']));
    s.receive({ type: 'events', events: [write(5, 'x')] });
    expect(ids(s)).toEqual(['a', 'x', 'b']);
    expect(s.lastSeq).toBe(6);
  });

  it('puts events that arrive out of order back in order', () => {
    const s = new ClassStore();
    s.receive(snap(0));
    s.receive({ type: 'events', events: [write(2, 'second')] });
    expect(s.hasGap()).toBe(true);
    expect(ids(s)).toEqual([]);
    s.receive({ type: 'events', events: [write(1, 'first')] });
    expect(s.hasGap()).toBe(false);
    expect(ids(s)).toEqual(['first', 'second']);
  });

  it('recovers from a lost event with a new snapshot', () => {
    const s = new ClassStore();
    s.receive(snap(0));
    s.receive({ type: 'events', events: [write(2, 'b')] });
    s.lose();
    s.receive({ type: 'events', events: [write(4, 'd')] });
    s.receive(snap(3, ['a', 'b', 'c']));
    expect(ids(s)).toEqual(['a', 'b', 'c', 'd']);
    expect(s.hasGap()).toBe(false);
  });

  it('marks and clears a named part', () => {
    const s = new ClassStore();
    s.receive(snap(0, ['g']));
    s.receive({
      type: 'events',
      events: [{ seq: 1, t: 0, type: 'board.mark', style: 'highlight', id: 'g', part: 'inner' }],
    });
    expect(s.state.board.lines[0].marks).toEqual({ inner: 'highlight' });
    s.receive({
      type: 'events',
      events: [{ seq: 2, t: 0, type: 'board.mark', style: 'clear', id: 'g', part: 'inner' }],
    });
    expect(s.state.board.lines[0].marks).toEqual({});
  });
  it('keeps a wiped page and brings it back', () => {
    const s = new ClassStore();
    s.receive(snap(0, ['a']));
    s.receive({
      type: 'events',
      events: [
        { seq: 1, t: 0, type: 'board.clear' },
        write(2, 'b'),
        { seq: 3, t: 0, type: 'board.show', page: 1 },
        write(4, 'c'),
      ],
    });
    expect(s.state.board.page).toBe(1);
    expect(ids(s)).toEqual(['a', 'c']);
    s.receive({ type: 'events', events: [{ seq: 5, t: 0, type: 'board.show', page: 2 }] });
    expect(ids(s)).toEqual(['b']);
    s.receive({ type: 'events', events: [{ seq: 6, t: 0, type: 'board.clear' }] });
    expect(s.state.board.page).toBe(3);
    expect(Object.keys(s.state.board.saved).sort()).toEqual(['1', '2']);
  });
  it('follows the stage: slides, marks and writing on a slide, and a split', () => {
    const s = new ClassStore();
    s.receive(snap(0));
    const deck: Deck = {
      id: 'd',
      title: 'D',
      slides: [
        { title: 'One', items: [], areas: {} },
        {
          title: 'Two',
          items: [],
          areas: { work: [0, 0, 1, 1] },
        },
      ],
    };
    s.receive({
      type: 'events',
      events: [
        { seq: 1, t: 0, type: 'slides.load', deck },
        {
          seq: 2,
          t: 0,
          type: 'stage.show',
          rows: [[{ source: 'slides', slide: 1 }, { source: 'board' }], [{ source: 'screen' }]],
        },
        { seq: 3, t: 0, type: 'slides.mark', style: 'highlight', name: 'inside' },
        { seq: 4, t: 0, type: 'slides.go', n: 2 },
        { seq: 5, t: 0, type: 'slides.go', n: 9 },
        { seq: 6, t: 0, type: 'slides.write', id: 'w', kind: 'math', content: 'x', area: 'work' },
        { seq: 7, t: 0, type: 'slides.mark', style: 'box', id: 'w' },
        {
          seq: 8,
          t: 0,
          type: 'screen.state',
          on: true,
          tabs: [{ title: 'Wikipedia', url: 'https://en.wikipedia.org/' }],
          active: 0,
        },
      ],
    });
    const stage = s.state.stage;
    expect(stage.rows.map((row) => row.map((pane) => pane.source))).toEqual([
      ['slides', 'board'],
      ['screen'],
    ]);
    expect(stage.slide).toBe(2);
    expect(stage.marks).toEqual({ 1: { inside: 'highlight' } });
    expect(stage.ink[2].map((ln) => [ln.id, ln.area, ln.marks])).toEqual([
      ['w', 'work', { '': 'box' }],
    ]);
    expect(stage.screen.on).toBe(true);
  });
  it('starts again when the lecturer restarts, and notices a lost last event', () => {
    const s = new ClassStore();
    s.receive({ ...snap(40, ['old']), epoch: 'a' });
    s.receive({ type: 'tick', epoch: 'a', seq: 41 });
    expect(s.hasGap()).toBe(true); // event 41 never came
    s.receive({ type: 'events', epoch: 'b', events: [write(2, 'new2')] });
    expect(s.needsSnapshot()).toBe(true);
    s.receive({ ...snap(1, ['new1']), epoch: 'b' });
    expect(ids(s)).toEqual(['new1', 'new2']);
    expect(s.hasGap()).toBe(false);
  });
  it('lays a second column level with the line it was put beside', () => {
    const line = (id: string, more: object = {}) => ({
      id,
      kind: 'math' as const,
      content: id,
      marks: {},
      ...more,
    });
    const rows = boardRows([
      line('_title', { kind: 'title' as const }),
      line('a'),
      line('b'),
      line('c'),
      line('n', { col: 1, beside: 'b' }),
      line('m', { col: 1 }),
      line('d'),
    ]);
    expect(Object.fromEntries(rows)).toEqual({ _title: 1, a: 2, b: 3, c: 4, n: 3, m: 4, d: 5 });
  });
});

describe('the schedule', () => {
  it('opens a room ten minutes early and closes it fifteen minutes after the end', async () => {
    const { stateOf } = await import('../schedule');
    const starts = Date.parse('2026-12-02T19:00:00+11:00');
    const session = {
      id: 's',
      kind: 'class' as const,
      title: 't',
      about: '',
      lesson: 'c/l',
      starts,
      minutes: 30,
    };
    const min = 60_000;
    expect(stateOf(session, starts - 11 * min)).toBe('soon');
    expect(stateOf(session, starts - 9 * min)).toBe('open');
    expect(stateOf(session, starts + 44 * min)).toBe('open');
    expect(stateOf(session, starts + 46 * min)).toBe('over');
  });
});
