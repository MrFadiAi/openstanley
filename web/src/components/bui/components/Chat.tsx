/*
 * Chat — Beautiful UI (https://www.beautifului.dev/)
 * MIT License — Copyright (c) 2026 Shane Levine. See ../LICENSE.
 * Adapted for OpenStanley: the tabbed-panel grammar of the demo (tab
 * header, sectioned reply rows, user bubble) exported as reusable
 * pieces. The Write page composes them around real streaming state.
 */
"use client";

/* ─────────────────────────────────────────────────────────
 * CHAT — tabbed chat panel pieces.
 *   ChatTabs    the Reply / Reasoning tab header
 *   ChatSection a labelled reasoning row (source · what ran)
 *   UserBubble  right-aligned soft prompt block
 * ───────────────────────────────────────────────────────── */

import type { ReactNode } from "react";

export function ChatTabs({
  tabs,
  active,
  onChange,
  trailing,
}: {
  tabs: { key: string; label: string }[];
  active: string;
  onChange: (key: string) => void;
  trailing?: ReactNode;
}) {
  return (
    <div className="flex shrink-0 items-center justify-between border-b border-line p-1.5">
      <div className="flex items-center">
        {tabs.map((tab) => (
          <button
            key={tab.key}
            type="button"
            aria-pressed={active === tab.key}
            onClick={() => onChange(tab.key)}
            className={`rounded-[6px] px-2 py-[3px] text-[13px] text-ink transition-[background-color,opacity] duration-100 ${
              active === tab.key ? "bg-field" : "opacity-50 hover:opacity-75"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>
      {trailing && <div className="flex items-center gap-1">{trailing}</div>}
    </div>
  );
}

export function ChatSection({
  label,
  sub,
  time,
  body,
  resolving,
}: {
  label: string;
  sub?: string;
  time?: string;
  body: ReactNode;
  resolving?: boolean;
}) {
  return (
    <div
      className="flex w-full flex-col gap-1.5 transition-[opacity,filter,transform] duration-400"
      style={{
        opacity: resolving ? 0.55 : 1,
        filter: resolving ? "blur(0.5px)" : "blur(0)",
        transform: resolving ? "scale(0.985)" : "scale(1)",
        transformOrigin: "top left",
        transitionTimingFunction: "cubic-bezier(0.23, 1, 0.32, 1)",
        animation: "fade-up 400ms cubic-bezier(0.23,1,0.32,1) both",
      }}
    >
      <div className="flex items-center gap-1 text-[12px] leading-[1.3]">
        <span className="font-medium text-ink">{label}</span>
        {sub && <span className="text-ink-2">{sub}</span>}
        {time && <span className="text-ink">for {time}</span>}
      </div>
      <p className="text-[13px] leading-normal text-ink">{body}</p>
    </div>
  );
}

export function UserBubble({ children }: { children: ReactNode }) {
  return (
    <div className="flex justify-end ps-14">
      <div
        className="rounded-xl bg-field px-3 py-1.5 text-[13px] leading-[1.4] text-ink"
        style={{
          animation: "fade-up 300ms cubic-bezier(0.23,1,0.32,1) both",
        }}
      >
        {children}
      </div>
    </div>
  );
}
