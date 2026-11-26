'use client';

import * as React from 'react';
import { BoardView } from './BoardView';
import { ScreenView, ShareView } from './ScreenView';
import { SlidesView } from './SlidesView';
import type { BoardState, Pane, StageState } from './types';
import { VideoPane } from './VideoView';

function PaneView({ pane, board, stage }: { pane: Pane; board: BoardState; stage: StageState }) {
  if (pane.source === 'board') return <BoardView board={board} page={pane.page} />;
  if (pane.source === 'slides') return <SlidesView stage={stage} slide={pane.slide} />;
  if (pane.source === 'share') return <ShareView who={pane.who} name={pane.name} />;
  if (pane.source === 'video') return <VideoPane video={stage.video} />;
  return <ScreenView screen={stage.screen} />;
}

/**
 * What the class is looking at. The lecturer splits the stage into rows of panes,
 * left to right as it names them: the board, slides, a shared screen, a video, or a
 * particular slide or board page.
 */
export function StageView({ board, stage }: { board: BoardState; stage: StageState }) {
  return (
    <div
      className="ch-stage-rows"
      style={{ gridTemplateRows: `repeat(${stage.rows.length}, minmax(0, 1fr))` }}
    >
      {stage.rows.map((row, r) => (
        <div
          key={r}
          className="ch-stage-row"
          style={{ gridTemplateColumns: `repeat(${row.length}, minmax(0, 1fr))` }}
        >
          {row.map((pane, c) => (
            // Keyed by what the pane shows, so a pane that only moves keeps its state.
            <div
              key={`${pane.source}:${pane.slide ?? pane.page ?? pane.who ?? ''}:${c}`}
              className="ch-pane"
              data-source={pane.source}
            >
              <PaneView pane={pane} board={board} stage={stage} />
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
