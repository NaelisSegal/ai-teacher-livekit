'use client';

import katex from 'katex';
import * as React from 'react';
import { boardRows } from './store';
import type { BoardLine, BoardState } from './types';

/**
 * `\part{name}{...}` names a piece of an equation so the lecturer can mark it later.
 * It becomes a span with data-part="name". Nothing else from the lecturer is trusted
 * as HTML.
 */
export const KATEX_OPTIONS: katex.KatexOptions = {
  displayMode: true,
  fleqn: true,
  throwOnError: false,
  strict: 'ignore',
  macros: { '\\part': '\\htmlData{part=#1}{#2}' },
  trust: (context) => context.command === '\\htmlData',
};

/**
 * An equation as HTML. KaTeX can throw on some input even when told not to (a bad
 * part name, for one). One bad line must not take the class page down, so it is
 * then shown as it was written.
 */
export function renderMath(latex: string): string {
  try {
    return katex.renderToString(latex, KATEX_OPTIONS);
  } catch {
    const span = document.createElement('span');
    span.textContent = latex;
    return span.outerHTML;
  }
}

/**
 * Where a picture is fetched from. The lecturer names it as lesson-files/... (lesson
 * prep) or class-files/... (made during this class). Anything else is not fetched.
 */
export function pictureAddress(content: string): string | null {
  return /^(lesson-files|class-files)(\/\w[\w.-]*){2,4}$/.test(content) ? `/${content}` : null;
}

/** A picture on the board or a slide. While it is being made, its place is kept. */
function Picture({ line }: { line: BoardLine }) {
  if (line.failed) return null;
  const address = line.pending ? null : pictureAddress(line.content);
  if (!address) {
    return (
      <div className="ch-line ch-line-image ch-line-pending">
        {line.pending ? 'A picture is being made.' : 'This picture cannot be shown.'}
      </div>
    );
  }
  return (
    <div className="ch-line ch-line-image">
      <img src={address} alt={line.alt ?? ''} />
    </div>
  );
}

/** The box around everything drawn for a named part, relative to `row`. */
function partBox(part: Element, row: DOMRect) {
  // Only what is inked counts: KaTeX also has tall invisible struts.
  const inked = [...part.querySelectorAll('*')].filter(
    (el) =>
      (el.childElementCount === 0 && el.textContent !== '') ||
      el.matches('.frac-line, .overline-line, .underline-line, svg'),
  );
  let box: { left: number; top: number; right: number; bottom: number } | null = null;
  for (const el of inked) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    box = box
      ? {
          left: Math.min(box.left, r.left),
          top: Math.min(box.top, r.top),
          right: Math.max(box.right, r.right),
          bottom: Math.max(box.bottom, r.bottom),
        }
      : { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
  }
  if (!box) return null;
  const pad = 3;
  return {
    left: box.left - row.left - pad,
    top: box.top - row.top - pad,
    width: box.right - box.left + pad * 2,
    height: box.bottom - box.top + pad * 2,
  };
}

function Line({ line }: { line: BoardLine }) {
  const row = React.useRef<HTMLDivElement>(null);
  const layer = React.useRef<HTMLDivElement>(null);
  const html = React.useMemo(
    () => (line.kind === 'math' ? renderMath(line.content) : null),
    [line.kind, line.content],
  );

  // Marks on named parts are drawn as boxes behind the equation, sized from where the
  // part actually landed. (A fraction's own span is shorter than the fraction.)
  React.useLayoutEffect(() => {
    const rowEl = row.current;
    const layerEl = layer.current;
    if (!rowEl || !layerEl) return;
    const draw = () => {
      layerEl.replaceChildren();
      rowEl
        .querySelectorAll('[data-mark-part]')
        .forEach((p) => p.removeAttribute('data-mark-part'));
      const rowRect = rowEl.getBoundingClientRect();
      for (const [part, style] of Object.entries(line.marks)) {
        if (!part) continue;
        const found = rowEl.querySelectorAll(`.katex-html [data-part="${CSS.escape(part)}"]`);
        for (const el of found) {
          el.setAttribute('data-mark-part', style);
          const box = partBox(el, rowRect);
          if (!box) continue;
          const mark = document.createElement('div');
          mark.className = `ch-part-mark ch-part-mark-${style}`;
          Object.assign(mark.style, {
            left: `${box.left}px`,
            top: `${box.top}px`,
            width: `${box.width}px`,
            height: `${box.height}px`,
          });
          layerEl.append(mark);
        }
      }
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(rowEl);
    return () => observer.disconnect();
  }, [line.marks, html]);

  const reveal = line.revealMs
    ? ({ animationDuration: `${line.revealMs}ms` } as React.CSSProperties)
    : undefined;
  const className = `ch-line ch-line-${line.kind}${line.revealMs ? ' ch-reveal' : ''}`;

  return (
    <div ref={row} className="ch-line-row" data-mark={line.marks['']}>
      <div ref={layer} className="ch-part-marks" aria-hidden="true" />
      {line.kind === 'image' ? (
        <Picture line={line} />
      ) : html !== null ? (
        <div className={className} style={reveal} dangerouslySetInnerHTML={{ __html: html }} />
      ) : (
        <div className={className} style={reveal}>
          {line.content}
        </div>
      )}
    </div>
  );
}

export const MemoLine = React.memo(Line);

const NO_LINES: BoardLine[] = [];

export function BoardView({ board, page: fixed }: { board: BoardState; page?: number }) {
  const end = React.useRef<HTMLDivElement>(null);
  // A pane may be fixed to one page of the board. Otherwise it shows the current one.
  const page = fixed ?? board.page;
  const lines = page === board.page ? board.lines : (board.saved[page] ?? NO_LINES);
  const count = lines.length;
  const rows = React.useMemo(() => boardRows(lines), [lines]);
  const lastRow = Math.max(0, ...rows.values());

  // Keep the newest line in view, as a lecturer would by moving down the board.
  React.useEffect(() => {
    end.current?.scrollIntoView({ block: 'end', behavior: 'smooth' });
  }, [count, page]);

  return (
    <div className="ch-board" aria-label="Board">
      <div className="ch-board-page">
        {lines.map((line) => (
          <div
            key={`${page}:${line.id}`}
            className={`ch-board-cell${line.space ? ' ch-board-space' : ''}`}
            style={{
              gridRow: rows.get(line.id),
              gridColumn: line.kind === 'title' ? '1 / -1' : line.col === 1 ? 2 : 1,
            }}
          >
            <MemoLine line={line} />
          </div>
        ))}
        <div ref={end} style={{ gridRow: lastRow + 1, gridColumn: '1 / -1' }} />
      </div>
    </div>
  );
}
