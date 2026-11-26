'use client';

import { Room } from 'livekit-client';
import * as React from 'react';
import { askLecturer } from './lecturer';
import type { Poll } from './types';
import { CHAT_MAX_CHARS, type ChatLine } from './useChat';

/** The lecturer's latest poll, pinned at the top of the chat. Totals, no names. */
function PollCard({ room, poll }: { room: Room; poll: Poll }) {
  const [mine, setMine] = React.useState<number | null>(null);
  const total = poll.counts.reduce((a, b) => a + b, 0);
  const vote = async (option: number) => {
    if (!poll.open) return;
    setMine(option);
    await askLecturer(room, 'ch.vote', JSON.stringify({ poll: poll.id, option }));
  };
  return (
    <div className="ch-poll">
      <div className="ch-poll-head">{poll.open ? 'Poll' : 'Poll, closed'}</div>
      <div className="ch-poll-question">{poll.question}</div>
      {poll.options.map((option, i) => {
        const share = total ? Math.round((poll.counts[i] / total) * 100) : 0;
        return (
          <button
            key={i}
            className="ch-poll-option"
            aria-pressed={mine === i}
            disabled={!poll.open}
            onClick={() => vote(i)}
          >
            <span className="ch-poll-bar" style={{ width: `${share}%` }} />
            <span className="ch-poll-text">{option}</span>
            <span className="ch-poll-count">{poll.counts[i]}</span>
          </button>
        );
      })}
    </div>
  );
}

export function ChatPanel({
  room,
  chat,
  polls,
}: {
  room: Room;
  chat: { lines: ChatLine[]; send: (text: string) => Promise<void> };
  polls: Poll[];
}) {
  const { lines, send } = chat;
  const [draft, setDraft] = React.useState('');
  const list = React.useRef<HTMLDivElement>(null);
  const poll = polls[polls.length - 1];

  React.useEffect(() => {
    list.current?.scrollTo({ top: list.current.scrollHeight });
  }, [lines.length]);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const text = draft;
    setDraft('');
    send(text).catch((err) => {
      console.warn('chat message was not sent', err);
      setDraft(text);
    });
  };

  return (
    <aside className="ch-chat" aria-label="Chat">
      {poll ? <PollCard key={poll.id} room={room} poll={poll} /> : null}
      <div className="ch-chat-list" ref={list}>
        {lines.length === 0 ? (
          <p className="ch-chat-empty">Questions and comments go here.</p>
        ) : (
          lines.map((line) => (
            <p key={line.id} className={line.mine ? 'ch-chat-mine' : undefined}>
              <strong>{line.name}</strong> {line.text}
            </p>
          ))
        )}
      </div>
      <form onSubmit={submit}>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          maxLength={CHAT_MAX_CHARS}
          placeholder="Type a question"
          aria-label="Chat message"
        />
        <button className="ch-button" disabled={!draft.trim()}>
          Send
        </button>
      </form>
    </aside>
  );
}
