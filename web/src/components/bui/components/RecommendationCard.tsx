/*
 * RecommendationCard — Beautiful UI (https://www.beautifului.dev/)
 * MIT License — Copyright (c) 2026 Shane Levine. See ../LICENSE.
 * Adapted for OpenStanley: the recommendation is a real scheduling
 * suggestion (best posting hour from the analytics heatmap) with a
 * confidence meter; alternatives are the runner-up hours.
 */
"use client";

import { useState } from "react";

/* ─────────────────────────────────────────────────────────
 * RECOMMENDATION CARD
 * The card holds its shape. Pressing "Alternatives" opens a
 * drawer listing the other options; picking one promotes
 * it to the recommendation. The primary action confirms.
 * ───────────────────────────────────────────────────────── */

export interface RecOption {
  key: string;
  body: React.ReactNode;
  short: string;
  /** 0–3 bars on the confidence meter */
  signal: number;
  tone: string;
  label: string;
}

function Meter({ signal, tone }: { signal: number; tone: string }) {
  return (
    <span className="flex items-end gap-0.5">
      {[0, 1, 2].map((bar) => (
        <span
          key={bar}
          className="w-1 rounded-full transition-colors duration-300"
          style={{ height: 10, background: bar < signal ? tone : "var(--line-strong)" }}
        />
      ))}
    </span>
  );
}

export default function RecommendationCard({
  title,
  options,
  cta,
  acceptedLabel,
  alternativesLabel,
  onAccept,
  disabled = false,
  emptyHint,
}: {
  title: string;
  options: RecOption[];
  cta: string;
  acceptedLabel: string;
  alternativesLabel: string;
  onAccept: (key: string) => void;
  disabled?: boolean;
  emptyHint?: string;
}) {
  const [selected, setSelected] = useState(0);
  const [open, setOpen] = useState(false);
  const [accepted, setAccepted] = useState(false);

  if (options.length === 0) {
    return (
      <div className="w-full overflow-hidden rounded-card bg-surface shadow-card">
        <div className="primitive-card-pad">
          <span className="text-[13px] font-semibold text-ink">{title}</span>
          <p className="mt-1.5 text-[12.5px] leading-relaxed text-ink-2">
            {emptyHint ?? "—"}
          </p>
        </div>
      </div>
    );
  }

  const active = options[selected];
  const others = options.map((o, i) => ({ o, i })).filter(({ i }) => i !== selected);

  return (
    <div className="w-full overflow-hidden rounded-card bg-surface shadow-card">
      <div className="primitive-card-pad">
        <span className="text-[13px] font-semibold text-ink">{title}</span>
        <p
          key={active.key}
          className="mt-1.5 min-h-12 text-[13px] leading-relaxed text-ink-2"
          style={{ animation: "fade-in 180ms ease-out both" }}
        >
          {active.body}
        </p>
      </div>

      {/* alternatives drawer — a distinctly new section of the card */}
      {others.length > 0 && (
        <div
          className="grid transition-[grid-template-rows,opacity] duration-300"
          style={{
            gridTemplateRows: open ? "1fr" : "0fr",
            opacity: open ? 1 : 0,
            transitionTimingFunction: "cubic-bezier(0.16, 1, 0.3, 1)",
          }}
        >
          <div className="overflow-hidden">
            <div className="border-t border-line bg-inset px-2 py-2">
              <p className="px-1.5 pb-1 text-[11px] font-medium text-ink-3">
                {alternativesLabel}
              </p>
              {others.map(({ o, i }) => (
                <button
                  key={o.key}
                  type="button"
                  onClick={() => {
                    setSelected(i);
                    setAccepted(false);
                    setOpen(false);
                  }}
                  className="flex w-full items-center gap-2.5 rounded-control px-1.5 py-1.5
                    text-start transition-colors duration-100 hover:bg-hover"
                >
                  <Meter signal={o.signal} tone={o.tone} />
                  <span className="min-w-0 flex-1 truncate text-[12.5px] text-ink">{o.short}</span>
                  <span className="shrink-0 text-[11px] text-ink-3">{o.label}</span>
                </button>
              ))}
            </div>
          </div>
        </div>
      )}

      <div className="primitive-card-footer flex items-center justify-between gap-3 border-t border-line bg-inset">
        <span className="flex items-center gap-2">
          <Meter signal={active.signal} tone={active.tone} />
          <span className="text-[12.5px] font-medium text-ink-2">{active.label}</span>
        </span>

        <span className="-me-0.5 flex items-center gap-2">
          {others.length > 0 && (
            <button
              type="button"
              aria-expanded={open}
              onClick={() => setOpen((current) => !current)}
              className="h-7 rounded-control px-2.5 text-[12.5px] font-medium shadow-btn
                transition-[background-color,transform] duration-100 active:scale-[0.96]
                bg-surface text-ink hover:bg-hover"
            >
              {alternativesLabel}
            </button>
          )}
          <button
            type="button"
            disabled={disabled}
            onClick={() => {
              setAccepted(true);
              onAccept(active.key);
            }}
            className="h-7 rounded-control bg-accent px-3 text-[12.5px] font-medium text-white
              shadow-[inset_0_1px_0_rgba(255,255,255,0.14)] transition-[background-color,transform] duration-150
              active:scale-[0.96] disabled:pointer-events-none disabled:opacity-45"
          >
            {accepted ? acceptedLabel : cta}
          </button>
        </span>
      </div>
    </div>
  );
}
