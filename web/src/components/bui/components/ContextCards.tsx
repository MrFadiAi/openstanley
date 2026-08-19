/*
 * ContextCards — Beautiful UI (https://www.beautifului.dev/)
 * MIT License — Copyright (c) 2026 Shane Levine. See ../LICENSE.
 * Adapted for OpenStanley: chunks are the real retrieved context (voice
 * rubric, deep-scan fingerprint, idea bank, strategy) with source
 * badges and relevance from the thinking_steps SSE event.
 */
"use client";

import { useEffect, useState } from "react";

/* ─────────────────────────────────────────────────────────
 * CONTEXT CARDS
 * Retrieved chunks enter once, then remain available.
 * ───────────────────────────────────────────────────────── */

export interface ContextChunkCard {
  title: string;
  body: string;
  source: string;
  badge: string;
  relevance?: number;
}

const BADGE_TONES: Record<string, string> = {
  VOICE: "bg-accent",
  SCAN: "bg-green",
  IDEAS: "bg-orange",
  STRAT: "bg-red",
};

export default function ContextCards({
  chunks,
  title,
  totalLabel,
}: {
  chunks: ContextChunkCard[];
  title: string;
  totalLabel?: string;
}) {
  const [chipsShown, setChipsShown] = useState(false);

  useEffect(() => {
    const chips = setTimeout(() => setChipsShown(true), 350);
    return () => clearTimeout(chips);
  }, []);

  if (chunks.length === 0) return null;

  return (
    <div className="flex w-full flex-col gap-2">
      <div
        className="flex items-center gap-2 px-0.5"
        style={{ animation: "fade-in 400ms ease-out both" }}
      >
        <span className="text-[13px] font-semibold text-ink">{title}</span>
        <span className="inline-flex h-5 items-center rounded-md bg-inset px-1.5 text-[11.5px] font-medium text-ink-2 shadow-hairline tabular-nums">
          {totalLabel ?? chunks.length}
        </span>
      </div>

      {chunks.map((chunk, i) => (
        <div
          key={`${chunk.title}-${i}`}
          className="overflow-hidden rounded-card bg-surface shadow-card"
          style={{
            animation: `fade-up 400ms cubic-bezier(0.23,1,0.32,1) ${i * 100}ms both`,
          }}
        >
          <div className="primitive-card-bar flex items-center gap-2.5 border-b border-line">
            <span className="flex min-w-0 items-center gap-1.5 text-[13px] font-medium text-ink">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"><path d="M4 6h16M4 12h16M4 18h10" /></svg>
              <span className="truncate">{chunk.title}</span>
            </span>
            {chunk.relevance !== undefined && (
              <span className="ms-auto shrink-0 font-mono text-[11px] text-ink-3 tabular-nums">
                {chunk.relevance}%
              </span>
            )}
          </div>
          <p className="px-3 pt-2 pb-1 text-[12.5px] leading-relaxed text-ink-2">
            {chunk.body}
          </p>
          <div className="px-3 pb-3">
            <span
              className="inline-flex h-6 items-center gap-1.5 rounded-full bg-inset px-2
                text-[12px] font-medium text-ink-2 shadow-btn
                transition-[opacity,transform,background-color] duration-300 hover:bg-hover"
              style={{
                opacity: chipsShown ? 1 : 0,
                transform: chipsShown ? "scale(1)" : "scale(0.95)",
                transitionTimingFunction: "cubic-bezier(0.23, 1, 0.32, 1)",
                transitionDelay: `${i * 80}ms`,
              }}
            >
              <span className={`flex size-3.5 items-center justify-center rounded-[4px] ${BADGE_TONES[chunk.badge] ?? "bg-ink-3"} text-[7px] font-bold text-white`}>
                {chunk.badge}
              </span>
              {chunk.source}
              <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M7 17L17 7M7 7h10v10" /></svg>
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}
