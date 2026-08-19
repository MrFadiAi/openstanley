/*
 * TaskRows — Beautiful UI (https://www.beautifului.dev/)
 * MIT License — Copyright (c) 2026 Shane Levine. See ../LICENSE.
 * Adapted for OpenStanley: rows are the live agent loops (study/create/
 * engage/publish…) fed by /api/loops/status — last run, next run.
 */
"use client";

import { useState } from "react";

/* ─────────────────────────────────────────────────────────
 * TASK ROWS (Capsules)
 * Live loop states as capsule rows: green check = ran ok,
 * red X = errored, hollow ring = idle, spinner = running.
 * Expand for last-run detail + next scheduled run.
 * ───────────────────────────────────────────────────────── */

function SpinnerRing({ active, children }: { active?: boolean; children?: React.ReactNode }) {
  const size = 24, stroke = 2;
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  return (
    <span className="relative inline-flex shrink-0 items-center justify-center" style={{ width: size, height: size }}>
      <svg
        width={size} height={size} className="absolute inset-0"
        style={active ? { animation: "spin 1.1s linear infinite" } : undefined}
      >
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--line)" strokeWidth={stroke} />
        {active && (
          <circle
            cx={size / 2} cy={size / 2} r={r} fill="none"
            stroke="var(--ink-3)" strokeWidth={stroke} strokeLinecap="round"
            strokeDasharray={`${c * 0.28} ${c * 0.72}`}
          />
        )}
      </svg>
      <span className="relative text-[10.5px] font-semibold tabular-nums text-ink">{children}</span>
    </span>
  );
}

function Badge({ tone, children }: { tone: "red" | "green"; children: React.ReactNode }) {
  return (
    <span
      className={`flex size-5.5 shrink-0 items-center justify-center rounded-full text-white
        ${tone === "red" ? "bg-red" : "bg-green"}`}
      style={{ animation: "pop-in 300ms cubic-bezier(0.23,1,0.32,1) both" }}
    >
      {children}
    </span>
  );
}

const XIcon = (
  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round"><path d="M18 6L6 18M6 6l12 12" /></svg>
);
const CheckIcon = (
  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
);

export type TaskStatus = "idle" | "running" | "ok" | "error";

export interface TaskRowItem {
  key: string;
  label: string;
  amount?: string;
  status: TaskStatus;
  pill?: React.ReactNode;
  details: { label: string; meta: string }[];
}

export default function TaskRows({ tasks }: { tasks: TaskRowItem[] }) {
  const [manualOpen, setManualOpen] = useState<Record<string, boolean>>({});

  return (
    <div className="flex w-full flex-col gap-2">
      {tasks.map((row, i) => {
        const open = manualOpen[row.key] ?? false;
        return (
          <div
            key={row.key}
            className="self-stretch overflow-hidden bg-surface shadow-card transition-[border-radius] duration-300"
            style={{
              borderRadius: open ? 14 : 22,
              animation: `fade-up 450ms cubic-bezier(0.23,1,0.32,1) ${i * 80}ms both`,
            }}
          >
            <button
              type="button"
              aria-expanded={open}
              onClick={() => setManualOpen((current) => ({ ...current, [row.key]: !open }))}
              className="flex h-11 w-full items-center gap-2.5 px-2.5 text-start transition-colors duration-100 hover:bg-inset"
            >
              <span className="flex size-6 shrink-0 items-center justify-center">
                {row.status === "ok" ? (
                  <Badge tone="green">{CheckIcon}</Badge>
                ) : row.status === "error" ? (
                  <Badge tone="red">{XIcon}</Badge>
                ) : (
                  <SpinnerRing active={row.status === "running"}>{i + 1}</SpinnerRing>
                )}
              </span>
              <span className={`min-w-0 flex-1 truncate text-[13px] font-medium ${row.status === "idle" ? "text-ink-2" : "text-ink"}`}>
                {row.label}
              </span>
              {row.amount && (
                <span className="text-[12.5px] text-ink-2 tabular-nums">{row.amount}</span>
              )}
              {row.pill}
              <span
                aria-hidden="true"
                className="-ml-2 flex size-7 shrink-0 items-center justify-center rounded-full text-ink-3"
              >
                <svg
                  width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
                  className="transition-transform duration-300"
                  style={{ transform: open ? "rotate(180deg)" : "rotate(0)" }}
                >
                  <path d="M6 9l6 6 6-6" />
                </svg>
              </span>
            </button>

            {/* dropdown detail — same expandable grammar as Chain of Thought */}
            <div
              className="grid transition-[grid-template-rows,opacity] duration-300"
              style={{
                gridTemplateRows: open ? "1fr" : "0fr",
                opacity: open ? 1 : 0,
                transitionTimingFunction: "cubic-bezier(0.23, 1, 0.32, 1)",
              }}
            >
              <div className="overflow-hidden">
                <div className="mb-2.5 grid grid-cols-[24px_1fr] gap-2.5 px-2.5">
                  <span aria-hidden className="mx-auto h-full w-px bg-line" />
                  <div className="flex flex-col gap-1.5">
                    {row.details.map((d, j) => (
                      <div
                        key={d.label}
                        className="flex items-center justify-between gap-2"
                        style={
                          open
                            ? { animation: `fade-up 300ms cubic-bezier(0.23,1,0.32,1) ${120 + j * 100}ms both` }
                            : undefined
                        }
                      >
                        <span className="min-w-0 truncate text-[12px] text-ink-2">{d.label}</span>
                        <span className="shrink-0 font-mono text-[11.5px] text-ink-3 tabular-nums">
                          {d.meta}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
