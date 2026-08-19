/*
 * CodeBlock — Beautiful UI (https://www.beautifului.dev/)
 * MIT License — Copyright (c) 2026 Shane Levine. See ../LICENSE.
 * Adapted for OpenStanley: "show me the raw post" — renders thread JSON /
 * raw draft text with line numbers and a live copy button. Lines fade
 * in staggered (no demo loop); JSON gets light token coloring.
 */
"use client";

import { useCallback, useEffect, useState } from "react";

/* ─────────────────────────────────────────────────────────
 * CODE BLOCK
 * Raw payload with line numbers; copy is live.
 * ───────────────────────────────────────────────────────── */

type Tok = { t: string; c?: "key" | "str" | "num" };

const COLORS: Record<string, string> = {
  key: "var(--accent-ink)",
  str: "var(--green)",
  num: "var(--orange)",
};

/** naive JSON tokenizer per line — enough for raw draft/thread dumps */
function tokenizeJson(line: string): Tok[] {
  const out: Tok[] = [];
  const re = /("(?:[^"\\]|\\.)*")(\s*:)?|(-?\d+(?:\.\d+)?)|(\btrue\b|\bfalse\b|\bnull\b)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(line)) !== null) {
    if (m.index > last) out.push({ t: line.slice(last, m.index) });
    if (m[1] !== undefined) out.push(m[2] ? { t: m[1] + m[2], c: "key" } : { t: m[1], c: "str" });
    else if (m[3] !== undefined) out.push({ t: m[3], c: "num" });
    else if (m[4] !== undefined) out.push({ t: m[4], c: "key" });
    last = m.index + m[0].length;
  }
  if (last < line.length) out.push({ t: line.slice(last) });
  return out.length ? out : [{ t: line }];
}

export default function CodeBlock({
  filename,
  language,
  code,
  json = true,
}: {
  filename: string;
  language?: string;
  code: string;
  /** tokenize as JSON when true; plain mono text otherwise */
  json?: boolean;
}) {
  const [shown, setShown] = useState(0);
  const [copied, setCopied] = useState(false);
  const lines = code.split("\n");
  const done = shown >= lines.length;

  useEffect(() => {
    if (done) return;
    const t = setTimeout(() => setShown((c) => c + 1), 90);
    return () => clearTimeout(t);
  }, [shown, done]);

  const copy = useCallback(() => {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  }, [code]);

  return (
    <div className="w-full overflow-hidden rounded-card bg-surface shadow-card">
      {/* header */}
      <div className="primitive-card-bar flex items-center justify-between border-b border-line">
        <span className="flex items-baseline gap-2">
          <span className="font-mono text-[12px] font-medium text-ink">{filename}</span>
          {language && <span className="text-[11.5px] text-ink-3">{language}</span>}
        </span>
        <button
          aria-label="Copy code"
          onClick={copy}
          className={`flex h-6 items-center gap-1 rounded-[6px] px-1.5 text-[11.5px]
            font-medium transition-colors duration-100 hover:bg-hover
            ${copied ? "text-green" : "text-ink-3 hover:text-ink"}`}
        >
          {copied ? (
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
          ) : (
            <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="12" height="12" rx="2.5" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
          )}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>

      {/* code */}
      <pre className="max-h-72 overflow-auto bg-inset px-3 py-2.5 font-mono text-[11.5px] leading-[1.7]">
        {lines.slice(0, shown + 1).map((line, i) => (
          <div
            key={i}
            className="flex"
            style={{ animation: "fade-up 250ms cubic-bezier(0.23,1,0.32,1) both" }}
          >
            <span className="w-5 shrink-0 select-none text-end text-[10.5px] leading-[1.86] text-ink-3/60">
              {i + 1}
            </span>
            <span className="whitespace-pre ps-2.5">
              {(json ? tokenizeJson(line) : [{ t: line } as Tok]).map((tok, j) => (
                <span key={j} style={{ color: tok.c ? COLORS[tok.c] : "var(--ink-2)" }}>
                  {tok.t}
                </span>
              ))}
              {i === shown && !done && (
                <span className="ms-0.5 inline-block h-3 w-[3px] translate-y-0.5 rounded-full bg-accent" />
              )}
            </span>
          </div>
        ))}
      </pre>
    </div>
  );
}
