/*
 * ApprovalCard — Beautiful UI (https://www.beautifului.dev/)
 * MIT License — Copyright (c) 2026 Shane Levine. See ../LICENSE.
 * Adapted for OpenStanley: THE approval gate for a post candidate —
 * question, the post itself, algorithm score + voice match context,
 * accept (saves an approved draft) / regenerate (decline-with-variant).
 * Nothing ever posts from here; only the calendar + publish loop send.
 */
"use client";

import { useState, type ReactNode } from "react";

/* ─────────────────────────────────────────────────────────
 * APPROVAL CARD (human-in-the-loop)
 * One candidate at a time. Footer carries the score chips;
 * the primary pill accepts, the secondary rolls a variant.
 * ───────────────────────────────────────────────────────── */

export default function ApprovalCard({
  question,
  children,
  scoreChip,
  voiceChip,
  factors,
  factorsLabel,
  acceptLabel,
  regenLabel,
  discardLabel,
  savedLabel,
  altHint,
  disabled = false,
  onAccept,
  onRegenerate,
  onDismiss,
}: {
  question: string;
  /** the post text itself (direction handled by the caller) */
  children: ReactNode;
  scoreChip?: ReactNode;
  voiceChip?: ReactNode;
  /** factor notes revealed under the post */
  factors?: { name: string; impact: number; note?: string }[];
  factorsLabel?: string;
  acceptLabel: string;
  regenLabel: string;
  discardLabel?: string;
  savedLabel: string;
  altHint?: string;
  disabled?: boolean;
  onAccept: () => void | Promise<void>;
  onRegenerate?: () => void;
  onDismiss?: () => void;
}) {
  const [sent, setSent] = useState(false);
  const [dismissed, setDismissed] = useState(false);

  if (dismissed) {
    return (
      <button
        type="button"
        onClick={() => setDismissed(false)}
        className="rounded-control bg-surface px-3 py-2 text-[12.5px] font-medium text-ink shadow-btn transition-colors duration-150 hover:bg-hover"
      >
        {discardLabel ?? question}
      </button>
    );
  }

  const accept = (): void => {
    setSent(true);
    void onAccept();
  };

  return (
    <div className="w-full">
      <div className="w-full self-start overflow-hidden rounded-card bg-surface shadow-card">
        {sent ? (
          <div className="flex h-37 flex-col items-center justify-center gap-2">
            <span
              className="flex size-6 items-center justify-center rounded-full bg-green text-white"
              style={{ animation: "pop-in 300ms cubic-bezier(0.23,1,0.32,1) both" }}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
            </span>
            <span className="text-[13px] font-medium text-ink" style={{ animation: "fade-up 350ms cubic-bezier(0.23,1,0.32,1) 100ms both" }}>
              {savedLabel}
            </span>
          </div>
        ) : (
          <div
            className="primitive-card-pad"
            style={{ animation: "fade-up 350ms cubic-bezier(0.23,1,0.32,1) both" }}
          >
            <div className="flex items-start justify-between gap-3">
              <span className="text-[13px] font-medium text-ink">{question}</span>
              {onDismiss && (
                <button
                  type="button"
                  aria-label={discardLabel ?? "Dismiss"}
                  onClick={() => setDismissed(true)}
                  className="primitive-icon-button shrink-0 text-ink-3 transition-colors duration-100 hover:bg-hover hover:text-ink"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                    <path d="M18 6L6 18M6 6l12 12" />
                  </svg>
                </button>
              )}
            </div>
            {children}
            {(factors?.length ?? 0) > 0 && (
              <div className="mt-2 flex flex-col gap-0.5 border-t border-line pt-2">
                <span className="text-[11px] font-medium text-ink-3">{factorsLabel}</span>
                {factors?.slice(0, 4).map((f, i) => (
                  <div key={i} className="flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate text-[12px] text-ink-2">{f.name}</span>
                    <span className={`shrink-0 font-mono text-[11.5px] tabular-nums ${f.impact >= 0 ? "text-green" : "text-red"}`}>
                      {f.impact >= 0 ? `+${f.impact}` : f.impact}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* footer — score chips + accept / regenerate */}
        <div className="primitive-card-footer flex items-center justify-between gap-2 border-t border-line bg-inset">
          <span className="flex min-w-0 items-center gap-1.5">
            {scoreChip}
            {voiceChip}
          </span>
          {!sent && (
            <span className="-me-0.5 flex shrink-0 items-center gap-2">
              {onRegenerate && (
                <button
                  type="button"
                  title={altHint}
                  disabled={disabled}
                  onClick={onRegenerate}
                  className="h-7 rounded-control bg-surface px-2.5 text-[12.5px] font-medium text-ink shadow-btn
                    transition-[background-color,transform] duration-100 hover:bg-hover active:scale-[0.96]
                    disabled:pointer-events-none disabled:opacity-45"
                >
                  {regenLabel}
                </button>
              )}
              <button
                type="button"
                disabled={disabled}
                onClick={accept}
                className="h-7 rounded-control bg-ink px-3 text-[12.5px] font-medium text-canvas
                  shadow-[inset_0_1px_0_rgba(255,255,255,0.14)] transition-[background-color,transform] duration-150
                  active:scale-[0.96] disabled:pointer-events-none disabled:opacity-45"
              >
                {acceptLabel}
              </button>
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
