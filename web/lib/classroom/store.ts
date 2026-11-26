import type {
  BoardLine,
  BoardState,
  ClassEvent,
  ClassMessage,
  ClassState,
  FloorState,
  Poll,
  Snapshot,
  StageState,
  VideoState,
} from './types';

export const noVideo: VideoState = { src: null, playing: false, position: 0, quiet: false, at: 0 };

export const emptyStage: StageState = {
  rows: [[{ source: 'board' }]],
  deck: null,
  slide: 1,
  ink: {},
  marks: {},
  screen: { on: false, tabs: [], active: 0 },
  video: noVideo,
};

export const emptyFloor: FloorState = { mode: 'hands', prompt: '', hands: [], speaker: null };

/** Apply one floor event. The same rules as agent/src/lecturer/room/floor.py. */
export function reduceFloor(floor: FloorState, ev: ClassEvent): FloorState {
  switch (ev.type) {
    case 'floor.mode':
      return {
        ...floor,
        mode: ev.mode,
        prompt: ev.prompt ?? '',
        hands: ev.mode === 'hands' ? floor.hands : [],
      };
    case 'floor.hand': {
      const hands = floor.hands.filter((h) => h.identity !== ev.identity);
      return {
        ...floor,
        hands: ev.up ? [...hands, { identity: ev.identity, name: ev.name }] : hands,
      };
    }
    case 'floor.give':
      return {
        ...floor,
        hands: floor.hands.filter((h) => h.identity !== ev.identity),
        speaker: { identity: ev.identity, name: ev.name },
      };
    case 'floor.take':
      return { ...floor, speaker: null };
    case 'floor.pause':
      return { ...floor, paused: ev.on };
    default:
      return floor;
  }
}

/** Apply one poll event. The same rules as agent/src/lecturer/room/polls.py. */
export function reducePolls(polls: Poll[], ev: ClassEvent): Poll[] {
  switch (ev.type) {
    case 'poll.open':
      return [
        ...polls.map((p) => ({ ...p, open: false })),
        {
          id: ev.id,
          question: ev.question,
          options: ev.options,
          counts: ev.options.map(() => 0),
          open: true,
        },
      ].slice(-3);
    case 'poll.results':
      return polls.map((p) => (p.id === ev.id ? { ...p, counts: ev.counts } : p));
    case 'poll.close':
      return polls.map((p) =>
        p.id === ev.id ? { ...p, counts: ev.counts ?? p.counts, open: false } : p,
      );
    default:
      return polls;
  }
}

export const emptyState: ClassState = {
  synced: false,
  phase: 'waiting',
  plan: {},
  board: { page: 0, top: 0, lines: [], saved: {} },
  stage: emptyStage,
  floor: emptyFloor,
  polls: [],
  caption: null,
  pause: null,
  split: null,
  asks: [],
  group: null,
};

/** The saved pages, with the page showing now put away among them. */
function keep(board: BoardState): BoardState['saved'] {
  if (board.lines.length === 0) return { ...board.saved };
  // A page that comes back is already written, so it is not drawn in again.
  const settled = board.lines.map(({ revealMs: _, ...line }) => line);
  return { ...board.saved, [board.page]: settled };
}

/**
 * Which row of the board each line sits on. Lines go down the first column in the
 * order written. A second column starts level with the line it was put beside (or
 * at the top) and goes down from there. A title has a row to itself.
 */
export function boardRows(lines: BoardLine[]): Map<string, number> {
  const rows = new Map<string, number>();
  let left = 0;
  let right = 0;
  for (const line of lines) {
    let row: number;
    if (line.kind === 'title') {
      row = Math.max(left, right) + 1;
      left = right = row;
    } else if (line.col === 1) {
      const level = line.beside ? rows.get(line.beside) : undefined;
      if (level !== undefined) right = Math.max(right, level - 1);
      row = ++right;
    } else {
      row = ++left;
    }
    rows.set(line.id, row);
  }
  return rows;
}

/** Apply one board event. The same rules as agent/src/lecturer/room/board.py. */
export function reduceBoard(board: BoardState, ev: ClassEvent): BoardState {
  switch (ev.type) {
    case 'board.clear':
      return { page: board.top + 1, top: board.top + 1, lines: [], saved: keep(board) };
    case 'board.show': {
      if (ev.page === board.page) return board;
      const back = board.saved[ev.page];
      if (!back && ev.page > board.top) return board;
      const saved = keep(board);
      delete saved[ev.page];
      return { ...board, page: ev.page, lines: back ?? [], saved };
    }
    case 'board.write':
      return {
        ...board,
        lines: [
          ...board.lines,
          {
            id: ev.id,
            kind: ev.kind,
            content: ev.content,
            marks: {},
            revealMs: ev.reveal_ms,
            col: ev.col ?? 0,
            beside: ev.beside ?? '',
            space: ev.space ?? false,
            ...(ev.kind === 'image' ? { alt: ev.alt ?? '', pending: ev.pending ?? false } : {}),
          },
        ],
      };
    case 'board.fill': {
      // The picture may be ready after its page was wiped: look on every page.
      const fill = (lines: BoardLine[]) =>
        lines.map((ln) =>
          ln.id === ev.id && ln.pending
            ? { ...ln, content: ev.content, pending: false, failed: ev.failed ?? false }
            : ln,
        );
      const saved: BoardState['saved'] = {};
      for (const [page, lines] of Object.entries(board.saved)) saved[page] = fill(lines);
      return { ...board, lines: fill(board.lines), saved };
    }
    case 'board.erase':
      return { ...board, lines: board.lines.filter((ln) => ln.id !== ev.id) };
    case 'board.mark': {
      const part = ev.part ?? '';
      return {
        ...board,
        lines: board.lines.map((ln) => {
          if (ev.style === 'clear' && !ev.id) return { ...ln, marks: {} };
          if (ln.id !== ev.id) return ln;
          const marks = { ...ln.marks };
          if (ev.style === 'clear') delete marks[part];
          else marks[part] = ev.style;
          return { ...ln, marks };
        }),
      };
    }
    default:
      return board;
  }
}

/** Apply one stage event. The same rules as agent/src/lecturer/room/stage.py. */
export function reduceStage(
  stage: StageState,
  ev: ClassEvent,
  now: number = Date.now(),
): StageState {
  switch (ev.type) {
    case 'video.load':
      return {
        ...stage,
        video: { src: ev.src, playing: false, position: ev.position ?? 0, quiet: false, at: now },
      };
    case 'video.play':
      return {
        ...stage,
        video: {
          ...stage.video,
          playing: true,
          position: ev.position,
          quiet: ev.quiet ?? false,
          at: now,
        },
      };
    case 'video.stop':
      return {
        ...stage,
        video: { ...stage.video, playing: false, position: ev.position, quiet: false, at: now },
      };
    case 'slides.fill': {
      // The picture may be ready after the lecturer has moved to another slide.
      const ink: StageState['ink'] = {};
      for (const [n, lines] of Object.entries(stage.ink)) {
        ink[n] = lines.map((ln) =>
          ln.id === ev.id && ln.pending
            ? { ...ln, content: ev.content, pending: false, failed: ev.failed ?? false }
            : ln,
        );
      }
      return { ...stage, ink };
    }
    case 'stage.show':
      return { ...stage, rows: ev.rows };
    case 'slides.load':
      return { ...stage, deck: ev.deck, slide: 1, ink: {}, marks: {} };
    case 'slides.go':
      return ev.n >= 1 && ev.n <= (stage.deck?.slides.length ?? 0)
        ? { ...stage, slide: ev.n }
        : stage;
    case 'slides.write': {
      const line = {
        id: ev.id,
        kind: ev.kind,
        content: ev.content,
        area: ev.area,
        marks: {},
        revealMs: ev.reveal_ms,
        ...(ev.kind === 'image' ? { alt: ev.alt ?? '', pending: ev.pending ?? false } : {}),
      };
      return {
        ...stage,
        ink: { ...stage.ink, [stage.slide]: [...(stage.ink[stage.slide] ?? []), line] },
      };
    }
    case 'slides.mark': {
      const n = stage.slide;
      const part = ev.part ?? '';
      if (ev.id) {
        const lines = (stage.ink[n] ?? []).map((ln) => {
          if (ln.id !== ev.id) return ln;
          const marks = { ...ln.marks };
          if (ev.style === 'clear') delete marks[part];
          else marks[part] = ev.style;
          return { ...ln, marks };
        });
        return { ...stage, ink: { ...stage.ink, [n]: lines } };
      }
      if (ev.name) {
        const marks = { ...(stage.marks[n] ?? {}) };
        if (ev.style === 'clear') delete marks[ev.name];
        else marks[ev.name] = ev.style;
        return { ...stage, marks: { ...stage.marks, [n]: marks } };
      }
      if (ev.style !== 'clear') return stage;
      const lines = (stage.ink[n] ?? []).map((ln) => ({ ...ln, marks: {} }));
      return { ...stage, ink: { ...stage.ink, [n]: lines }, marks: { ...stage.marks, [n]: {} } };
    }
    case 'screen.state':
      return {
        ...stage,
        screen: { on: ev.on, tabs: ev.tabs ?? [], active: ev.active ?? 0, failed: ev.failed },
      };
    default:
      return stage;
  }
}

function reduce(state: ClassState, ev: ClassEvent, now: number): ClassState {
  switch (ev.type) {
    case 'say':
      return { ...state, caption: { id: ev.id, text: ev.text, done: false }, pause: null };
    case 'heard':
    case 'cut':
      return state.caption?.id === ev.id
        ? { ...state, caption: { ...state.caption, done: true } }
        : state;
    case 'pause':
      return {
        ...state,
        pause: {
          text: ev.text ?? '',
          seconds: ev.seconds,
          endsAt: now + ev.seconds * 1000,
          watching: ev.watching ?? false,
        },
      };
    case 'pause.end':
      return { ...state, pause: null };
    case 'plan':
      return { ...state, plan: { segment: ev.segment, title: ev.title } };
    case 'phase':
      return { ...state, phase: ev.phase };
    case 'split.open':
      return {
        ...state,
        split: { how: ev.how, minutes: ev.minutes ?? 0, groups: ev.groups, to: ev.to ?? {} },
      };
    case 'split.assign':
      return state.split
        ? {
            ...state,
            split: { ...state.split, to: { ...state.split.to, [ev.identity]: ev.group } },
          }
        : state;
    case 'split.ended':
      return state.split
        ? {
            ...state,
            split: {
              ...state.split,
              groups: state.split.groups.map((g) =>
                g.id === ev.group ? { ...g, ended: true } : g,
              ),
            },
          }
        : state;
    case 'split.close':
      return { ...state, split: null };
    case 'split.ask': {
      const { type: _type, seq: _seq, t: _t, ...ask } = ev;
      return { ...state, asks: [...state.asks, ask] };
    }
    case 'split.asked':
      return { ...state, asks: state.asks.filter((ask) => ask.id !== ev.id) };
    case 'split.back':
      return state.group ? { ...state, group: { ...state.group, back: true } } : state;
    case 'poll.open':
    case 'poll.results':
    case 'poll.close':
      return { ...state, polls: reducePolls(state.polls, ev) };
    case 'floor.mode':
    case 'floor.hand':
    case 'floor.give':
    case 'floor.take':
    case 'floor.pause':
      return { ...state, floor: reduceFloor(state.floor, ev) };
    case 'stage.show':
    case 'slides.load':
    case 'slides.go':
    case 'slides.write':
    case 'slides.mark':
    case 'slides.fill':
    case 'screen.state':
    case 'video.load':
    case 'video.play':
    case 'video.stop':
      return { ...state, stage: reduceStage(state.stage, ev, now) };
    default:
      return { ...state, board: reduceBoard(state.board, ev) };
  }
}

function fromSnapshot(snap: Snapshot, now: number): ClassState {
  return {
    synced: true,
    phase: snap.phase,
    plan: snap.plan ?? {},
    board: {
      page: snap.board.page,
      top: snap.board.top ?? snap.board.page,
      lines: snap.board.lines.map((ln) => ({ ...ln })),
      saved: { ...(snap.board.saved ?? {}) },
    },
    // A snapshot gives the moment the video is at as it was made, which is now.
    stage: snap.stage
      ? { ...snap.stage, video: { ...noVideo, ...(snap.stage.video ?? {}), at: now } }
      : emptyStage,
    floor: snap.floor ?? emptyFloor,
    polls: snap.polls ?? [],
    split: snap.split ?? null,
    asks: snap.asks ?? [],
    group: snap.group ?? null,
    caption: snap.caption ? { ...snap.caption, done: false } : null,
    pause: snap.pause
      ? {
          text: snap.pause.text,
          seconds: snap.pause.seconds_left,
          endsAt: now + snap.pause.seconds_left * 1000,
          watching: snap.pause.watching ?? false,
        }
      : null,
  };
}

/**
 * Keeps class state in step with the lecturer.
 *
 * Events are applied strictly in sequence order. Ones that arrive early wait in a
 * buffer. `hasGap()` says an event seems to be missing; the caller then asks for a
 * new snapshot (after a short wait, in case it is only late).
 */
export class ClassStore {
  state: ClassState = emptyState;
  lastSeq = 0;
  /** The furthest sequence number the lecturer has said it reached. */
  private headSeq = 0;
  private epoch: string | undefined;
  private buffer = new Map<number, ClassEvent>();

  receive(msg: ClassMessage, now: number = Date.now()): void {
    if (msg.epoch !== undefined && msg.epoch !== this.epoch) {
      // A different run of the lecturer: its numbers start again, so start again too.
      if (this.epoch !== undefined) {
        this.state = { ...emptyState };
        this.lastSeq = 0;
        this.headSeq = 0;
        this.buffer.clear();
      }
      this.epoch = msg.epoch;
    }
    if (msg.type === 'tick') {
      this.headSeq = Math.max(this.headSeq, msg.seq);
    } else if (msg.type === 'snapshot') {
      // While we are in step, a snapshot from before our place has nothing new.
      // (While waiting for one, any snapshot is taken: later events are in the buffer.)
      if (this.state.synced && msg.seq < this.lastSeq) return;
      this.state = fromSnapshot(msg, now);
      this.lastSeq = msg.seq;
    } else {
      for (const ev of msg.events) {
        if (ev.seq > this.lastSeq) this.buffer.set(ev.seq, ev);
      }
    }
    this.drain(now);
  }

  private drain(now: number): void {
    if (!this.state.synced) return;
    for (const seq of this.buffer.keys()) {
      if (seq <= this.lastSeq) this.buffer.delete(seq);
    }
    let next: ClassEvent | undefined;
    while ((next = this.buffer.get(this.lastSeq + 1))) {
      this.buffer.delete(next.seq);
      this.state = reduce(this.state, next, now);
      this.lastSeq = next.seq;
    }
  }

  /** True when an event seems to be missing: one is waiting out of order, or the
   *  lecturer says it is further on than we are. */
  hasGap(): boolean {
    return this.state.synced && (this.buffer.size > 0 || this.headSeq > this.lastSeq);
  }

  /** True when we have never been in step, or lost our place. */
  needsSnapshot(): boolean {
    return !this.state.synced;
  }

  /** We lost our place (reconnect, or a gap that did not fill). Wait for a snapshot. */
  lose(): void {
    this.state = { ...this.state, synced: false };
  }
}
