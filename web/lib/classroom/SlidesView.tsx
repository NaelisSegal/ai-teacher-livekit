'use client';

import * as React from 'react';
import { MemoLine, renderMath } from './BoardView';
import type { MarkStyle, SlideItem, StageState, VideoState } from './types';
import { VideoView } from './VideoView';

function Item({ item, mark, video }: { item: SlideItem; mark?: MarkStyle; video: VideoState }) {
  const html = React.useMemo(
    () => (item.kind === 'math' ? renderMath(item.content) : null),
    [item.kind, item.content],
  );
  if (item.kind === 'image') {
    // Pictures come from the lesson's own folder, through /lesson-files.
    return (
      <div className="ch-slide-image">
        <img src={`/lesson-files/${item.content}`} alt="" />
      </div>
    );
  }
  if (item.kind === 'video') {
    return item.video ? (
      <div className="ch-slide-video">
        <VideoView src={item.video} video={video} />
      </div>
    ) : null;
  }
  const className = `ch-slide-item ch-slide-${item.kind}`;
  return html !== null ? (
    <div className={className} data-mark={mark} dangerouslySetInnerHTML={{ __html: html }} />
  ) : (
    <div className={className} data-mark={mark}>
      {item.content}
    </div>
  );
}

/** The slide showing now, with whatever the lecturer has marked and written on it. */
export function SlidesView({ stage, slide: fixed }: { stage: StageState; slide?: number }) {
  // A pane may be fixed to one slide. Otherwise it shows the current one.
  const number = fixed ?? stage.slide;
  const slide = stage.deck?.slides[number - 1];
  if (!stage.deck || !slide) {
    return <div className="ch-pane-empty">No slides are showing yet.</div>;
  }
  const marks = stage.marks[number] ?? {};
  const ink = stage.ink[number] ?? [];
  const loose = ink.filter((line) => !(line.area in slide.areas));

  return (
    <div className="ch-slides" aria-label="Slides">
      <div className="ch-slide">
        <div className="ch-slide-inner" key={`${stage.deck.id}:${number}`}>
          <h2 className="ch-slide-title">{slide.title}</h2>
          <div className="ch-slide-body">
            {slide.items.map((item, i) => (
              <Item
                key={i}
                item={item}
                mark={item.name ? marks[item.name] : undefined}
                video={stage.video}
              />
            ))}
          </div>
          {Object.entries(slide.areas).map(([name, [x, y, w, h]]) => (
            <div
              key={name}
              className="ch-slide-area"
              data-mark={marks[name]}
              style={{
                left: `${x * 100}%`,
                top: `${y * 100}%`,
                width: `${w * 100}%`,
                height: `${h * 100}%`,
              }}
            >
              {ink
                .filter((line) => line.area === name)
                .map((line) => (
                  <MemoLine key={line.id} line={line} />
                ))}
            </div>
          ))}
          {loose.length > 0 ? (
            <div className="ch-slide-loose">
              {loose.map((line) => (
                <MemoLine key={line.id} line={line} />
              ))}
            </div>
          ) : null}
        </div>
      </div>
      <div className="ch-slide-count">
        {number} / {stage.deck.slides.length}
      </div>
    </div>
  );
}
