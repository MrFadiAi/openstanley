/*
 * StreamingText — Beautiful UI (https://www.beautifului.dev/)
 * MIT License — Copyright (c) 2026 Shane Levine. See ../LICENSE.
 * Adapted for OpenStanley: real SSE tokens render through the caller's
 * markdown pipeline; the primitive owns the streaming feel — words
 * resolve out of blur as the container grows, a blinking caret runs
 * while tokens land, then action buttons + follow-up suggestions
 * unlock when the reply settles.
 */
"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";

/* ─────────────────────────────────────────────────────────
 * STREAMING TEXT
 * content: rendered reply so far (caller owns markdown).
 * streaming: caret on. done: actions + follow-ups unlock.
 * The last appended chunk resolves with stream-in blur.
 * ───────────────────────────────────────────────────────── */

export default function StreamingText({
  content,
  streaming,
  done,
  actions,
  followUps,
  onFollowUp,
  followUpsLabel,
  copyIcon,
  copyLabel,
  copiedLabel,
  onCopy,
}: {
  content: ReactNode;
  streaming: boolean;
  done: boolean;
  /** action icon buttons shown on the done row */
  actions?: ReactNode;
  followUps?: { key: string; label: string }[];
  onFollowUp?: (key: string) => void;
  followUpsLabel: string;
  copyIcon?: ReactNode;
  copyLabel: string;
  copiedLabel: string;
  onCopy?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const tailRef = useRef<HTMLSpanElement>(null);

  // re-fire the stream-in blur on the tail whenever content grows
  useEffect(() => {
    if (!streaming || !tailRef.current) return;
    tailRef.current.animate(
      [
        { opacity: 0, filter: "blur(4px)" },
        { opacity: 1, filter: "blur(0px)" },
      ],
      { duration: 320, easing: "cubic-bezier(0.22,0.61,0.25,1)" },
    );
  }, [content, streaming]);

  const copy = (): void => {
    if (!onCopy) return;
    onCopy();
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="w-full">
      <div className="text-[13px] leading-relaxed text-ink">
        {content}
        {streaming && (
          <span
            className="ms-0.5 inline-block h-3 w-0.5 translate-y-0.5 rounded-full bg-accent"
            style={{ animation: "caret-blink 900ms steps(1) infinite" }}
          />
        )}
      </div>

      {/* action icons row */}
      <div
        className="mt-2 flex items-center gap-0.5 transition-opacity duration-400"
        style={{ opacity: done ? 1 : 0, pointerEvents: done ? "auto" : "none" }}
      >
        {onCopy && (
          <button
            type="button"
            aria-label={copied ? copiedLabel : copyLabel}
            onClick={copy}
            className={`flex size-6 items-center justify-center rounded-[6px] transition-colors duration-100 hover:bg-hover-2 ${
              copied ? "text-green" : "text-ink-3 hover:text-ink-2"
            }`}
          >
            {copied ? (
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M20 6L9 17l-5-5" />
              </svg>
            ) : (
              copyIcon ?? (
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                  <rect x="9" y="9" width="12" height="12" rx="2.5" />
                  <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
                </svg>
              )
            )}
          </button>
        )}
        {actions}
      </div>

      {/* follow-ups */}
      {followUps && followUps.length > 0 && (
        <div
          className="mt-2.5 transition-opacity duration-400"
          style={{ opacity: done ? 1 : 0, pointerEvents: done ? "auto" : "none" }}
        >
          <p className="text-[12px] font-medium text-ink-2">{followUpsLabel}</p>
          <div className="mt-0.5 flex flex-col">
            {followUps.map((f, i) => (
              <button
                key={f.key}
                type="button"
                onClick={() => onFollowUp?.(f.key)}
                className="-mx-1.5 flex items-center gap-2 rounded-[7px] border-b border-line
                  px-1.5 py-1.5 text-start text-[12.5px] text-ink transition-colors
                  duration-100 hover:bg-hover-2"
                style={
                  done
                    ? { animation: `fade-up 350ms cubic-bezier(0.23,1,0.32,1) ${i * 90}ms both` }
                    : { opacity: 0 }
                }
              >
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="var(--ink-3)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
                  <path d="M9 10l-5 5 5 5" />
                  <path d="M20 4v7a4 4 0 0 1-4 4H4" />
                </svg>
                {f.label}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
