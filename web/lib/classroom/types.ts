// The class event stream, as sent by the lecturer on the `ch.class` topic.
// The other end is agent/src/lecturer/room/events.py; keep the two in step.

export const CLASS_TOPIC = 'ch.class';
export const SYNC_RPC = 'ch.sync';

export type LineKind = 'math' | 'text' | 'title' | 'image';
export type MarkStyle = 'highlight' | 'underline' | 'box' | 'dim';

export type BoardLine = {
  id: string;
  kind: LineKind;
  content: string;
  /** Part name ('' means the whole line) to style. */
  marks: Record<string, MarkStyle>;
  /** Set when the line was written while we watched, so it is drawn in left to right. */
  revealMs?: number;
  /** 0 is the first column, 1 the second. */
  col?: number;
  /** The line this one starts level with, for the first line of a second column. */
  beside?: string;
  /** A gap above, to set a new problem apart. */
  space?: boolean;
  /** A picture (kind 'image'): `content` is where it is fetched from, and this is
   *  what it shows, in words. */
  alt?: string;
  /** A picture that is being made, and is not there yet. */
  pending?: boolean;
  /** A picture that could not be made. Nothing is drawn for it. */
  failed?: boolean;
};

/** A video as the site that has it calls it. A file's id is `<folder>/<file>`. */
export type VideoSrc = {
  kind: 'file' | 'youtube' | 'twitch';
  id: string;
  /** Twitch: 'video', 'clip' or 'channel'. */
  form: string;
};

/** The one video. Each browser plays it for itself; the lecturer only says which
 *  video, whether it is playing, and from what moment. */
export type VideoState = {
  src: VideoSrc | null;
  playing: boolean;
  /** The moment it was at, in seconds, when this was said. */
  position: number;
  /** The lecturer is talking over it, so it is turned down. */
  quiet: boolean;
  /** When this was said, by this browser's clock. */
  at: number;
};

/** The page showing now, and the wiped pages, which are kept so the lecturer can go back. */
export type BoardState = {
  page: number;
  /** The newest page's number. */
  top: number;
  lines: BoardLine[];
  saved: Record<string, BoardLine[]>;
};

/** 'share' is a person's own screen: a guest lecturer's slides, or the helper's. */
export type Source = 'board' | 'slides' | 'screen' | 'share' | 'video';

/** One thing on the stage. A slides pane may be fixed to one slide, and a board
 *  pane to one page; otherwise it shows whichever is current. */
export type Pane = { source: Source; slide?: number; page?: number; who?: string; name?: string };

/** The shared screen's tabs, as a browser's tab strip would show them. */
export type ScreenState = {
  on: boolean;
  tabs: { title: string; url: string }[];
  active: number;
  /** The browser could not be started, or it stopped. */
  failed?: boolean;
};

export type SlideItem = {
  kind: 'text' | 'bullet' | 'math' | 'image' | 'video';
  /** Set when the lecturer can mark this item by name. */
  name: string;
  content: string;
  /** For a video. */
  video?: VideoSrc;
};

export type Slide = {
  title: string;
  items: SlideItem[];
  /** Empty areas the lecturer can write in: name to [x, y, w, h] as fractions of the slide. */
  areas: Record<string, [number, number, number, number]>;
};

export type Deck = { id: string; title: string; slides: Slide[] };

/** A line the lecturer wrote on a slide. */
export type InkLine = BoardLine & { area: string };

/** What is on the stage. The same rules as agent/src/lecturer/room/stage.py. */
export type StageState = {
  /** Rows of panes, left to right and top to bottom. */
  rows: Pane[][];
  deck: Deck | null;
  /** The number of the slide showing, from 1. */
  slide: number;
  ink: Record<string, InkLine[]>;
  marks: Record<string, Record<string, MarkStyle>>;
  screen: ScreenState;
  video: VideoState;
};

export type Person = { identity: string; name: string };
export type MicMode = 'closed' | 'hands' | 'open';

/** Who may speak. The same rules as agent/src/lecturer/room/floor.py. */
export type FloorState = {
  mode: MicMode;
  /** Shown with open mics: what the lecturer asked. */
  prompt: string;
  /** Raised hands, in order. */
  hands: Person[];
  /** The student who has the floor. */
  speaker: Person | null;
  /** A helper has paused the lecturer. */
  paused?: boolean;
};

/** A question from the lecturer with a few options, shown in the chat. */
export type Poll = {
  id: string;
  question: string;
  options: string[];
  counts: number[];
  open: boolean;
};

/** One group of a class that has split. It is a room of its own. */
export type SplitGroup = {
  id: string;
  title: string;
  about: string;
  room: string;
  ended?: boolean;
};

/** The class is in groups. The same rules as agent/src/lecturer/room/events.py. */
export type Split = {
  /** How students come to be in a group: they choose, by chance, a helper puts them
   *  there, or ('one') one student has been taken aside. */
  how: 'choose' | 'random' | 'helper' | 'one';
  minutes: number;
  groups: SplitGroup[];
  /** Who has been put where: identity to group id. */
  to: Record<string, string>;
};

/** Something helpers are asked to agree to: taking one student aside. */
export type Ask = { id: string; identity: string; name: string; title: string; why: string };

/** In a group's own room: which group this is, and the room it came from. */
export type GroupHere = { title: string; main: string; back: boolean };

export type Caption = { id: string; text: string; done: boolean };
export type Pause = { text: string; seconds: number; endsAt: number; watching?: boolean };
export type Phase = 'waiting' | 'teaching' | 'ended';

export type ClassState = {
  /** False until a snapshot has arrived, and again while we wait for a new one. */
  synced: boolean;
  phase: Phase;
  plan: { segment?: string; title?: string };
  board: BoardState;
  stage: StageState;
  floor: FloorState;
  polls: Poll[];
  caption: Caption | null;
  pause: Pause | null;
  split: Split | null;
  asks: Ask[];
  group: GroupHere | null;
};

export type ClassEvent = { seq: number; t: number } & (
  | { type: 'board.clear' }
  | { type: 'board.show'; page: number }
  | {
      type: 'board.write';
      id: string;
      kind: LineKind;
      content: string;
      reveal_ms?: number;
      col?: number;
      beside?: string;
      space?: boolean;
      alt?: string;
      pending?: boolean;
    }
  | { type: 'board.fill'; id: string; content: string; failed?: boolean }
  | { type: 'board.erase'; id: string }
  | { type: 'board.mark'; style: MarkStyle | 'clear'; id?: string; part?: string }
  | { type: 'stage.show'; rows: Pane[][] }
  | { type: 'slides.load'; deck: Deck }
  | { type: 'slides.go'; n: number }
  | {
      type: 'slides.write';
      id: string;
      kind: LineKind;
      content: string;
      area: string;
      reveal_ms?: number;
      alt?: string;
      pending?: boolean;
    }
  | { type: 'slides.fill'; id: string; content: string; failed?: boolean }
  | { type: 'video.load'; src: VideoSrc; position?: number }
  | { type: 'video.play'; position: number; quiet?: boolean }
  | { type: 'video.stop'; position: number }
  | { type: 'slides.mark'; style: MarkStyle | 'clear'; id?: string; part?: string; name?: string }
  | {
      type: 'screen.state';
      on: boolean;
      tabs?: ScreenState['tabs'];
      active?: number;
      failed?: boolean;
    }
  | { type: 'floor.mode'; mode: MicMode; prompt?: string }
  | { type: 'floor.hand'; identity: string; name: string; up: boolean }
  | { type: 'floor.give'; identity: string; name: string }
  | { type: 'floor.take'; identity: string }
  | { type: 'floor.pause'; on: boolean }
  | { type: 'poll.open'; id: string; question: string; options: string[] }
  | { type: 'poll.results'; id: string; counts: number[] }
  | { type: 'poll.close'; id: string; counts?: number[] }
  | { type: 'say'; id: string; text: string; source?: string; dur_ms?: number }
  | { type: 'heard'; id: string }
  | { type: 'cut'; id: string; heard_ms: number }
  | { type: 'pause'; seconds: number; text?: string; watching?: boolean }
  | { type: 'pause.end' }
  | { type: 'plan'; segment: string; title?: string }
  | { type: 'phase'; phase: Phase }
  | {
      type: 'split.open';
      how: Split['how'];
      minutes?: number;
      groups: SplitGroup[];
      to?: Split['to'];
    }
  | { type: 'split.assign'; identity: string; group: string }
  | { type: 'split.ended'; group: string }
  | { type: 'split.close' }
  | ({ type: 'split.ask' } & Ask)
  | { type: 'split.asked'; id: string; ok: boolean }
  // In place of an event only staff may see: it keeps the numbering whole.
  | { type: 'noop' }
  | { type: 'split.back'; room: string }
);

export type Snapshot = {
  type: 'snapshot';
  seq: number;
  phase: Phase;
  plan: { segment?: string; title?: string };
  board: {
    page: number;
    top: number;
    lines: Omit<BoardLine, 'revealMs'>[];
    saved: Record<string, Omit<BoardLine, 'revealMs'>[]>;
  };
  stage?: Omit<StageState, 'video'> & { video?: Omit<VideoState, 'at'> };
  floor?: FloorState;
  polls?: Poll[];
  caption: { id: string; text: string } | null;
  pause: { seconds_left: number; text: string; watching?: boolean } | null;
  split?: Split | null;
  asks?: Ask[];
  group?: GroupHere | null;
};

/**
 * `epoch` names one run of the lecturer. Sequence numbers start again when the
 * lecturer restarts, so a new epoch means: forget everything and take a snapshot.
 * A `tick` says how far the lecturer has got, so a lost last event is noticed.
 */
export type ClassMessage =
  | (Snapshot & { epoch?: string })
  | { type: 'events'; epoch?: string; events: ClassEvent[] }
  | { type: 'tick'; epoch?: string; seq: number };
