/*
 * FineTuneCard — Beautiful UI (https://www.beautifului.dev/)
 * MIT License — Copyright (c) 2026 Shane Levine. See ../LICENSE.
 * Adapted for OpenStanley: the "voice inspector" — temperature ladder,
 * formality / Arabic-mix / emoji-density scrub fields. Every change
 * persists through /api/settings and feeds draft generation params.
 */
"use client";

import { useRef, useState } from "react";

/* ─────────────────────────────────────────────────────────
 * FINE-TUNE CARD — compact interactive inspector.
 * Number fields scrub: hover the label for an ↔ cursor and
 * drag to adjust, use ↑/↓ (⇧ for ×10), or type directly.
 * ───────────────────────────────────────────────────────── */

function ScrubField({
  label,
  value,
  onChange,
  min,
  max,
  step = 1,
  suffix = "",
  active,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  min: number;
  max: number;
  step?: number;
  suffix?: string;
  active?: boolean;
}) {
  const drag = useRef<{ x: number; v: number } | null>(null);
  const clamp = (v: number) => Math.min(max, Math.max(min, Math.round(v)));

  return (
    <label
      className="flex h-6.5 min-w-0 items-center gap-1 rounded-chip py-1 pe-1 ps-0.5
        transition-[background-color,box-shadow] duration-200"
      style={{
        background: active ? "var(--accent-tint)" : "var(--field)",
        boxShadow: active ? "0 0 0 1px var(--accent)" : "none",
      }}
    >
      {/* scrub handle */}
      <span
        role="slider"
        aria-label={label}
        aria-valuenow={value}
        aria-valuemin={min}
        aria-valuemax={max}
        tabIndex={0}
        onPointerDown={(e) => {
          (e.target as HTMLElement).setPointerCapture(e.pointerId);
          drag.current = { x: e.clientX, v: value };
        }}
        onPointerMove={(e) => {
          if (!drag.current) return;
          onChange(clamp(drag.current.v + ((e.clientX - drag.current.x) / 2) * step));
        }}
        onPointerUp={() => (drag.current = null)}
        onKeyDown={(e) => {
          const mult = e.shiftKey ? 10 : 1;
          if (e.key === "ArrowUp" || e.key === "ArrowRight") {
            e.preventDefault();
            onChange(clamp(value + step * mult));
          } else if (e.key === "ArrowDown" || e.key === "ArrowLeft") {
            e.preventDefault();
            onChange(clamp(value - step * mult));
          }
        }}
        className="flex h-full shrink-0 cursor-ew-resize touch-none items-center rounded-[4px]
          px-0.5 text-[12px] text-ink-3 select-none hover:text-ink-2 focus-visible:text-accent-ink
          focus-visible:outline-none"
      >
        {label}
      </span>
      <input
        inputMode="numeric"
        value={value}
        onChange={(e) => {
          const n = Number(e.target.value.replace(/[^\d-]/g, ""));
          if (!Number.isNaN(n)) onChange(clamp(n));
        }}
        aria-label={`${label} value`}
        className="min-w-0 flex-1 bg-transparent text-[12px] text-ink tabular-nums outline-none"
      />
      {suffix && <span className="shrink-0 pe-0.5 text-[11.5px] text-ink-3">{suffix}</span>}
    </label>
  );
}

export interface VoiceTune {
  temperature: "safe" | "bold" | "experimental";
  formality: number;
  langMix: number;
  emoji: number;
}

const DEFAULT_TUNE: VoiceTune = { temperature: "bold", formality: 50, langMix: 50, emoji: 3 };

export default function FineTuneCard({
  tune,
  onChange,
  title,
  adjustingLabel,
  editedLabel,
  segLabels,
  fieldLabels,
  footerHint,
}: {
  tune: VoiceTune;
  onChange: (next: VoiceTune) => void;
  title: string;
  adjustingLabel: string;
  editedLabel: string;
  segLabels: [string, string, string];
  fieldLabels: { formality: string; langMix: string; emoji: string };
  footerHint: string;
}) {
  const seg = ["safe", "bold", "experimental"].indexOf(tune.temperature);
  const done =
    tune.temperature !== DEFAULT_TUNE.temperature ||
    tune.formality !== DEFAULT_TUNE.formality ||
    tune.langMix !== DEFAULT_TUNE.langMix ||
    tune.emoji !== DEFAULT_TUNE.emoji;
  const [flash, setFlash] = useState(false);

  const update = (patch: Partial<VoiceTune>): void => {
    onChange({ ...tune, ...patch });
    setFlash(true);
    setTimeout(() => setFlash(false), 1200);
  };

  return (
    <div className="relative w-full rounded-card bg-surface shadow-raised">
      {/* header */}
      <div className="primitive-card-bar flex items-center justify-between border-b border-line">
        <span className="text-[13px] font-medium text-ink">{title}</span>
        {done ? (
          <span
            className="flex items-center gap-1.5 text-[12px] font-medium text-green"
            style={{ animation: "pop-in 250ms cubic-bezier(0.23,1,0.32,1) both" }}
          >
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
              <path d="M20 6L9 17l-5-5" />
            </svg>
            {flash ? adjustingLabel : editedLabel}
          </span>
        ) : (
          <span className="flex items-center gap-1.5">
            <span className="flex size-4.5 items-center justify-center rounded-[5px] border border-accent/30 bg-accent-tint">
              <svg width="9" height="9" viewBox="0 0 24 24" fill="var(--accent)">
                <path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" />
              </svg>
            </span>
            <span
              className="bg-clip-text text-[12px] font-medium text-transparent"
              style={{
                backgroundImage:
                  "linear-gradient(90deg, var(--accent) 35%, var(--accent-ink) 50%, var(--accent) 65%)",
                backgroundSize: "200% 100%",
                animation: "shimmer-text 1.4s linear infinite",
              }}
            >
              {adjustingLabel}
            </span>
          </span>
        )}
      </div>

      {/* temperature ladder */}
      <div className="primitive-card-pad flex flex-col gap-2 border-b border-line">
        <div className="relative grid grid-cols-3 rounded-control bg-field p-0.5">
          <span
            aria-hidden
            className="absolute inset-y-0.5 rounded-[6px] bg-surface shadow-btn transition-transform duration-300"
            style={{
              width: "calc((100% - 4px) / 3)",
              left: 2,
              transform: `translateX(${Math.max(seg, 0) * 100}%)`,
              transitionTimingFunction: "cubic-bezier(0.23, 1, 0.32, 1)",
            }}
          />
          {(["safe", "bold", "experimental"] as const).map((s, i) => (
            <button
              key={s}
              type="button"
              aria-label={segLabels[i]}
              aria-pressed={i === seg}
              onClick={() => update({ temperature: s })}
              className={`relative z-10 flex h-6 items-center justify-center truncate px-1 text-[11.5px] font-medium transition-colors duration-200
                ${i === seg ? "text-accent" : "text-ink-3"}`}
            >
              {segLabels[i]}
            </button>
          ))}
        </div>
        <div className="grid min-w-0 grid-cols-2 gap-2">
          <ScrubField
            label={fieldLabels.formality}
            value={tune.formality}
            onChange={(formality) => update({ formality })}
            min={0}
            max={100}
            active={tune.formality !== DEFAULT_TUNE.formality}
          />
          <ScrubField
            label={fieldLabels.langMix}
            value={tune.langMix}
            onChange={(langMix) => update({ langMix })}
            min={0}
            max={100}
            suffix="%"
            active={tune.langMix !== DEFAULT_TUNE.langMix}
          />
        </div>
        <div className="grid min-w-0 grid-cols-2 gap-2">
          <ScrubField
            label={fieldLabels.emoji}
            value={tune.emoji}
            onChange={(emoji) => update({ emoji })}
            min={0}
            max={10}
            active={tune.emoji !== DEFAULT_TUNE.emoji}
          />
        </div>
      </div>

      {/* footer hint */}
      <div className="primitive-card-footer flex items-center justify-between">
        <span className="text-[12px] text-ink-3">{footerHint}</span>
      </div>
    </div>
  );
}
