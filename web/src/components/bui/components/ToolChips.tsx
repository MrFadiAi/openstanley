/*
 * ToolChips — Beautiful UI (https://www.beautifului.dev/)
 * MIT License — Copyright (c) 2026 Shane Levine. See ../LICENSE.
 * Adapted for OpenStanley: rows are real chat tool calls (schedule_draft,
 * pick_idea, query_analytics…) with their actual results; details render
 * the structured payload the backend returned.
 */
"use client";

import { useState } from "react";

/* ─────────────────────────────────────────────────────────
 * TOOL CHIPS
 * An agent run as compact rows: one chip per tool call with
 * a short argument summary; every row expands to show the
 * real result payload (✓ lines, or the error in red).
 * ───────────────────────────────────────────────────────── */

const Icons: Record<string, React.ReactNode> = {
  think: <path d="M12 2l2.4 7.2L22 12l-7.6 2.8L12 22l-2.4-7.2L2 12l7.6-2.8z" />,
  write: <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z" /></g>,
  run: <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 17l6-5-6-5M12 19h8" /></g>,
  read: <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /></g>,
};

/** tool name → glyph kind */
const TOOL_ICON: Record<string, string> = {
  schedule_draft: "write",
  create_quote_draft: "write",
  regenerate_draft: "write",
  pick_idea: "think",
  query_analytics: "run",
  scan_account: "read",
};

export interface ToolCall {
  name: string;
  args?: Record<string, unknown>;
  ok: boolean;
  result?: unknown;
}

/** short chip text: the most identifying argument */
function argSummary(tool: ToolCall): string {
  const a = tool.args ?? {};
  for (const k of ["text", "when", "timeframe", "tweet_url", "draft_id", "angle"]) {
    const v = a[k];
    if (typeof v === "string" && v.trim()) return v.length > 46 ? `${v.slice(0, 46)}…` : v;
    if (typeof v === "number") return `${k} ${v}`;
  }
  return tool.name;
}

/** flatten a result payload into display lines */
function resultLines(tool: ToolCall): { text: string; tone?: "add" | "err" }[] {
  const r = tool.result;
  if (!tool.ok) {
    const err = r && typeof r === "object" && "error" in r ? String((r as { error: unknown }).error) : "failed";
    return [{ text: `✗ ${err}`, tone: "err" }];
  }
  if (r === null || r === undefined || typeof r !== "object") {
    return [{ text: "✓ done", tone: "add" }];
  }
  const out: { text: string; tone?: "add" | "err" }[] = [];
  for (const [k, v] of Object.entries(r as Record<string, unknown>)) {
    if (k === "ok") continue;
    if (v === null || v === undefined) continue;
    const flat =
      typeof v === "string" || typeof v === "number" || typeof v === "boolean"
        ? String(v)
        : JSON.stringify(v);
    const short = flat.length > 90 ? `${flat.slice(0, 90)}…` : flat;
    out.push({ text: `✓ ${k}: ${short}`, tone: "add" });
    if (out.length >= 6) break;
  }
  return out.length ? out : [{ text: "✓ done", tone: "add" }];
}

export default function ToolChips({
  tools,
  headerLabel,
}: {
  tools: ToolCall[];
  headerLabel?: string;
}) {
  const [open, setOpen] = useState(true);
  const [openRows, setOpenRows] = useState<Set<string>>(new Set());
  if (tools.length === 0) return null;

  const toggleRow = (key: string) =>
    setOpenRows((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  return (
    <div className="w-full pb-1">
      {/* collapsed run header */}
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
        className="-mx-1.5 flex w-fit items-center gap-1.5 rounded-control px-1.5 py-1 text-[12.5px] text-ink-2 transition-colors duration-100 hover:bg-hover-2"
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" className="transition-transform duration-200" style={{ transform: open ? "rotate(0deg)" : "rotate(-90deg)" }}>
          <path d="M6 9l6 6 6-6" />
        </svg>
        <span className="tabular-nums">
          {headerLabel ?? `${tools.length} tool ${tools.length === 1 ? "call" : "calls"}`}
        </span>
      </button>

      {/* tool call rows */}
      <div className="grid transition-[grid-template-rows,opacity] duration-300" style={{ gridTemplateRows: open ? "1fr" : "0fr", opacity: open ? 1 : 0 }}>
        <div className="-mx-1 overflow-hidden px-1.5 pb-1">
          <div className="mt-1.5 flex flex-col gap-1">
            {tools.map((tool, idx) => {
              const key = `${tool.name}-${idx}`;
              const rowOpen = openRows.has(key);
              const icon = TOOL_ICON[tool.name] ?? "run";
              return (
                <div key={key} style={{ animation: "fade-up 300ms cubic-bezier(0.23,1,0.32,1) both" }}>
                  <button
                    type="button"
                    aria-expanded={rowOpen}
                    onClick={() => toggleRow(key)}
                    className="group/row -mx-[3px] flex h-7 w-[calc(100%+6px)] min-w-0 items-center gap-2 rounded-control px-[3px] text-start transition-colors duration-100 hover:bg-hover-2"
                  >
                    <span className="relative flex size-4 shrink-0 items-center justify-center text-ink-3">
                      <svg
                        width="13" height="13" viewBox="0 0 24 24" fill={icon === "think" ? "currentColor" : "none"} stroke="currentColor"
                        className={`transition-opacity duration-100 group-hover/row:opacity-0 ${rowOpen ? "opacity-0" : ""}`}
                      >
                        {Icons[icon]}
                      </svg>
                      <svg
                        width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
                        className={`absolute transition-[opacity,transform] duration-150 group-hover/row:opacity-100 ${rowOpen ? "opacity-100" : "opacity-0"}`}
                        style={{ transform: rowOpen ? "rotate(0deg)" : "rotate(-90deg)" }}
                      >
                        <path d="M6 9l6 6 6-6" />
                      </svg>
                    </span>
                    <span className={`shrink-0 text-[12.5px] font-medium ${tool.ok ? "text-ink" : "text-red"}`}>
                      {tool.name}
                    </span>
                    <span
                      className="inline-flex h-5.5 min-w-0 flex-1 cursor-pointer items-center truncate rounded-chip bg-hover-2 px-1.5
                        text-[11.5px] text-ink-2 shadow-hairline transition-colors duration-100 hover:bg-line-strong
                        dark:bg-field dark:hover:bg-hover"
                    >
                      {argSummary(tool)}
                    </span>
                  </button>

                  {/* expanded detail */}
                  <div
                    className="grid transition-[grid-template-rows,opacity] duration-300"
                    style={{ gridTemplateRows: rowOpen ? "1fr" : "0fr", opacity: rowOpen ? 1 : 0, transitionTimingFunction: "cubic-bezier(0.23, 1, 0.32, 1)" }}
                  >
                    <div className="min-h-0 overflow-hidden">
                      <div className="mt-0.5 mb-1 ms-2 flex flex-col gap-0.5 border-s border-line py-0.5 ps-3.5">
                        {resultLines(tool).map((line) => (
                          <span
                            key={line.text}
                            className={`break-all text-[11.5px] leading-[1.6] ${line.tone === "err" ? "text-red" : line.tone === "add" ? "text-green" : "text-ink-2"}`}
                          >
                            {line.text}
                          </span>
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
