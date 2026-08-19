/*
 * PromptBar — Beautiful UI (https://www.beautifului.dev/)
 * MIT License — Copyright (c) 2026 Shane Levine. See ../LICENSE.
 * Adapted for OpenStanley: the OpenStanley composer — @ references real
 * sources (niche accounts + idea bank), / expands real commands
 * (/draft /schedule /quote /scan /strategy /best-post), and the model
 * picker is the temperature ladder + reply language. Selecting
 * "experimental" fires the glimm rainbow sweep. Variant: Rounded.
 */
"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createShader, playSweep, accentChain, ACCENTS } from "glimm";

/* OpenStanley is a purple product — sweep in brand hues, not the whole rainbow. */
const SWEEP = accentChain([ACCENTS.purple, ACCENTS.blue, ACCENTS.cyan, ACCENTS.purple]);

/* ─────────────────────────────────────────────────────────
 * PROMPT BAR
 * A composer with real controls: @ data sources, / commands,
 * a generation picker (temperature ladder + language), send.
 * Type @ or / to open the menus; ↑↓ + Enter to pick.
 * ───────────────────────────────────────────────────────── */

function Icon({ children, size = 15, strokeWidth = 1.8 }: { children: React.ReactNode; size?: number; strokeWidth?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}

const GLYPHS: Record<string, React.ReactNode> = {
  at: <path d="M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm0 0v1.5A2.5 2.5 0 0 0 14.5 20 8.5 8.5 0 1 0 6 5.5" />,
  idea: <path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10c.6.6 1 1.2 1 2h6c0-.8.4-1.4 1-2a6 6 0 0 0-4-10z" />,
  slash: <g><path d="M4 20L20 4" /><rect x="3" y="3" width="18" height="18" rx="4" /></g>,
  clock: <g><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></g>,
  chart: <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" />,
  target: <g><circle cx="12" cy="12" r="9" /><circle cx="12" cy="12" r="5" /><circle cx="12" cy="12" r="1" /></g>,
};

export interface PromptSource {
  key: string;
  name: string;
  desc: string;
  glyph?: string;
}

export interface PromptCommand {
  key: string;
  name: string;
  desc: string;
  glyph?: string;
}

export type PromptTemperature = "safe" | "bold" | "experimental";
export type PromptLanguage = "auto" | "ar" | "en" | "mixed";

/* the last @word or /word being typed, if any */
function parseToken(draft: string): { kind: "at" | "slash"; query: string; start: number } | null {
  const match = /(^|\s)([@/])([\w-]*)$/.exec(draft);
  if (!match) return null;
  return {
    kind: match[2] === "@" ? "at" : "slash",
    query: match[3].toLowerCase(),
    start: match.index + match[1].length,
  };
}

export default function PromptBar({
  value,
  onChange,
  onSend,
  busy,
  sources,
  commands,
  temperature,
  onTemperature,
  language,
  onLanguage,
  placeholder,
  pickerLabels,
  noMatchesLabel,
  footerHint,
}: {
  value: string;
  onChange: (v: string) => void;
  onSend: (text: string) => void;
  busy: boolean;
  sources: PromptSource[];
  commands: PromptCommand[];
  temperature: PromptTemperature;
  onTemperature: (t: PromptTemperature) => void;
  language: PromptLanguage;
  onLanguage: (l: PromptLanguage) => void;
  placeholder: string;
  pickerLabels: {
    temp: string;
    temps: Record<PromptTemperature, string>;
    lang: string;
    langs: Record<PromptLanguage, string>;
  };
  noMatchesLabel: string;
  footerHint?: string;
}) {
  const [dismissed, setDismissed] = useState(false);
  const [plusOpen, setPlusOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [expanded, setExpanded] = useState(false);
  const [rowBox, setRowBox] = useState<{ top: number; height: number } | null>(null);
  const [engaged, setEngaged] = useState(false);
  const controlsRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const measureRef = useRef<HTMLSpanElement>(null);
  const pickerRef = useRef<HTMLButtonElement>(null);
  const rowRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const glimmRef = useRef<HTMLCanvasElement>(null);
  const shaderRef = useRef<ReturnType<typeof createShader> | null>(null);
  const sweepingRef = useRef(false);

  const draft = value;
  const setDraft = onChange;

  const token = dismissed ? null : parseToken(draft);
  const menu: "at" | "slash" | null = plusOpen ? "slash" : token?.kind ?? null;
  const query = plusOpen ? "" : token?.query ?? "";

  const rows: { key: string; name: string; desc: string; glyph?: string }[] =
    menu === "at"
      ? sources.filter((s) => s.name.toLowerCase().includes(query))
      : menu === "slash"
        ? commands.filter((c) => c.name.slice(1).startsWith(query))
        : [];

  useEffect(() => {
    setActive(0);
    setEngaged(false);
  }, [menu, query]);

  /* a single highlight glides to the active row instead of each row
   * toggling its own background */
  useLayoutEffect(() => {
    const target = rowRefs.current[active];
    if (target) setRowBox({ top: target.offsetTop, height: target.offsetHeight });
  }, [menu, query, active, rows.length]);

  /* glimm shader lives inside the composer, invisible at rest. Picking
   * "experimental" fires a one-shot brand sweep across the interior. */
  const makeShader = () => {
    const canvas = glimmRef.current;
    if (!canvas) return null;
    const random = Math.random;
    Math.random = () => 0;
    try {
      return createShader({
        canvas,
        palette: SWEEP,
        direction: "ltr",
        bandTight: 10,
        swellAmount: 0.85,
      });
    } finally {
      Math.random = random;
    }
  };

  useEffect(() => {
    shaderRef.current = makeShader();
    return () => {
      shaderRef.current?.destroy();
      shaderRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const celebrate = () => {
    if (sweepingRef.current) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    shaderRef.current?.destroy();
    const shader = makeShader();
    shaderRef.current = shader;
    if (!shader) return;
    sweepingRef.current = true;
    const sweep = playSweep(shader, {
      palette: SWEEP,
      direction: "ltr",
      sweepMs: 950,
      outroMs: 130,
      peakAlpha: 1.1,
      bandTight: 10,
      brightness: 1.3,
      swellAmount: 1,
      waveSpeed: 1.3,
      easing: "easeOutExpo",
    });
    sweep.done.finally(() => {
      sweepingRef.current = false;
    });
  };

  /* Move wrapped text above the controls, then grow to a compact maximum. */
  useLayoutEffect(() => {
    const input = inputRef.current;
    const controls = controlsRef.current;
    const measure = measureRef.current;
    const pickerButton = pickerRef.current;
    if (!input || !controls || !measure || !pickerButton) return;

    const fixedControlsWidth = 28 * 2 + pickerButton.offsetWidth;
    const inlineGaps = 4 * 3;
    const inlineInputWidth = controls.clientWidth - fixedControlsWidth - inlineGaps;
    const needsFullWidth = draft.includes("\n") || measure.offsetWidth + 8 > inlineInputWidth;
    if (needsFullWidth !== expanded) {
      setExpanded(needsFullWidth);
    }

    const minHeight = 28;
    const maxHeight = 120;
    input.style.height = "0px";
    const contentHeight = input.scrollHeight;
    input.style.height = `${Math.min(Math.max(contentHeight, minHeight), maxHeight)}px`;
    input.style.overflowY = contentHeight > maxHeight ? "auto" : "hidden";
  }, [draft, expanded]);

  const closeMenus = () => {
    setPlusOpen(false);
    setPickerOpen(false);
  };

  const pick = (row: { key: string; name: string }) => {
    const source = sources.find((s) => s.key === row.key);
    if (source) {
      setDraft(`${token ? draft.slice(0, token.start) : draft}@${row.name} `);
    } else {
      setDraft(`${token ? draft.slice(0, token.start) : draft}${row.name} `);
    }
    setPlusOpen(false);
    setDismissed(false);
    inputRef.current?.focus();
  };

  const canSend = draft.trim().length > 0 && !busy;
  const send = () => {
    if (!canSend) return;
    onSend(draft.trim());
    setDraft("");
    closeMenus();
  };

  const pickTemperature = (next: PromptTemperature) => {
    onTemperature(next);
    setPickerOpen(false);
    if (next === "experimental") celebrate();
    inputRef.current?.focus();
  };

  return (
    <div className="relative w-full">
      {/* ── @ / slash menu ─────────────────────────────── */}
      {menu && (
        <div
          onMouseLeave={() => setEngaged(false)}
          className="absolute inset-x-0 bottom-full z-10 mb-2 rounded-[10px] bg-surface p-1 shadow-raised"
          style={{ animation: "pop-in 180ms cubic-bezier(0.23,1,0.32,1) both", transformOrigin: "bottom center" }}
        >
          {/* single gliding highlight — appears once a row is hovered */}
          <span
            aria-hidden
            className="pointer-events-none absolute inset-x-1 rounded-[6px] bg-hover"
            style={{
              top: rowBox?.top ?? 0,
              height: rowBox?.height ?? 0,
              opacity: rowBox && engaged && rows.length > 0 ? 1 : 0,
              transition:
                "top 220ms cubic-bezier(0.23,1,0.32,1), height 220ms cubic-bezier(0.23,1,0.32,1), opacity 150ms ease",
            }}
          />
          {rows.map((row, i) => (
            <button
              key={row.key}
              type="button"
              ref={(el) => {
                rowRefs.current[i] = el;
              }}
              onMouseDown={(event) => event.preventDefault()}
              onMouseEnter={() => {
                setActive(i);
                setEngaged(true);
              }}
              onClick={() => pick(row)}
              className="relative z-10 flex h-9 w-full items-center gap-2.5 rounded-[6px] px-2 text-start"
            >
              <span className="flex size-5.5 shrink-0 items-center justify-center text-ink-2">
                <Icon size={15}>{GLYPHS[row.glyph ?? (menu === "at" ? "at" : "slash")]}</Icon>
              </span>
              <span className="shrink-0 text-[12.5px] font-medium text-ink">
                {row.name}
              </span>
              <span className="min-w-0 flex-1 truncate text-[12px] text-ink-3">{row.desc}</span>
            </button>
          ))}
          {rows.length === 0 && (
            <div className="flex h-9 items-center px-2 text-[12px] text-ink-3">
              {noMatchesLabel}
            </div>
          )}
          <div className="mt-1 border-t border-line px-2 pt-1.5 pb-1 text-[11px] text-ink-3">
            {menu === "at"
              ? `@ ${sources.length} sources`
              : `/ ${commands.length} commands`}
          </div>
        </div>
      )}

      {/* ── generation picker (temperature + language) ──── */}
      {pickerOpen && (
        <div
          onMouseLeave={() => setPickerOpen(false)}
          className="absolute end-0 bottom-full z-10 mb-2 w-52 rounded-[10px] bg-surface p-1 shadow-raised"
          style={{ animation: "pop-in 180ms cubic-bezier(0.23,1,0.32,1) both", transformOrigin: "bottom right" }}
        >
          <p className="px-2 pt-1 pb-0.5 text-[10.5px] font-medium uppercase tracking-[0.08em] text-ink-3">
            {pickerLabels.temp}
          </p>
          {(["safe", "bold", "experimental"] as const).map((t) => (
            <button
              key={t}
              type="button"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => pickTemperature(t)}
              className="flex h-7.5 w-full items-center gap-2 rounded-[6px] px-2 text-start hover:bg-hover"
            >
              <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-ink">
                {pickerLabels.temps[t]}
              </span>
              <span className={`shrink-0 text-ink ${temperature === t ? "" : "invisible"}`}>
                <Icon size={13} strokeWidth={2.5}><path d="M20 6L9 17l-5-5" /></Icon>
              </span>
            </button>
          ))}
          <div className="my-1 border-t border-line" />
          <p className="px-2 pt-1 pb-0.5 text-[10.5px] font-medium uppercase tracking-[0.08em] text-ink-3">
            {pickerLabels.lang}
          </p>
          {(["auto", "ar", "en", "mixed"] as const).map((l) => (
            <button
              key={l}
              type="button"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => {
                onLanguage(l);
                setPickerOpen(false);
                inputRef.current?.focus();
              }}
              className="flex h-7.5 w-full items-center gap-2 rounded-[6px] px-2 text-start hover:bg-hover"
            >
              <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-ink">
                {pickerLabels.langs[l]}
              </span>
              <span className={`shrink-0 text-ink ${language === l ? "" : "invisible"}`}>
                <Icon size={13} strokeWidth={2.5}><path d="M20 6L9 17l-5-5" /></Icon>
              </span>
            </button>
          ))}
        </div>
      )}

      {/* ── composer ───────────────────────────────────── */}
      <div
        className={`relative isolate flex flex-col gap-1.5 overflow-hidden border border-line bg-surface p-1.5 shadow-card transition-[border-color,border-radius] duration-150 focus-within:border-line-strong ${
          expanded ? "rounded-[14px]" : "rounded-[16px]"
        }`}
      >
        {/* glimm brand sweep — plays across the interior on experimental. */}
        <canvas
          ref={glimmRef}
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 -z-10 h-full w-full"
          style={{ borderRadius: "inherit" }}
        />
        <span
          ref={measureRef}
          aria-hidden="true"
          className="pointer-events-none absolute invisible whitespace-pre text-[13px] leading-[18px]"
        >
          {draft}
        </span>

        <div
          ref={controlsRef}
          className="grid grid-cols-[28px_minmax(0,1fr)_auto_28px] items-end gap-x-1 gap-y-1.5"
        >
          <button
            type="button"
            aria-label="Open commands"
            aria-expanded={plusOpen}
            onClick={() => {
              setPickerOpen(false);
              setPlusOpen((current) => !current);
              inputRef.current?.focus();
            }}
            className={`flex size-7 shrink-0 items-center justify-center justify-self-start rounded-[8px] text-ink-3 transition-[background-color,color,transform] duration-150 hover:bg-hover hover:text-ink active:scale-[0.94] ${
              plusOpen ? "bg-hover text-ink" : ""
            } ${expanded ? "col-start-1 row-start-2" : "col-start-1 row-start-1"}`}
          >
            <Icon size={16} strokeWidth={2}><path d="M12 5v14M5 12h14" /></Icon>
          </button>

          <textarea
            ref={inputRef}
            rows={1}
            dir="auto"
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value);
              setDismissed(false);
              setPlusOpen(false);
            }}
            onKeyDown={(event) => {
              if (menu && rows.length > 0) {
                if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                  event.preventDefault();
                  setEngaged(true);
                  setActive((current) => (current + (event.key === "ArrowDown" ? 1 : rows.length - 1)) % rows.length);
                  return;
                }
                if ((event.key === "Enter" && !event.shiftKey) || event.key === "Tab") {
                  event.preventDefault();
                  pick(rows[active]);
                  return;
                }
              }
              if (event.key === "Escape") {
                setDismissed(true);
                closeMenus();
                return;
              }
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                send();
              }
            }}
            placeholder={placeholder}
            aria-label="Prompt"
            className={`min-h-7 min-w-0 w-full resize-none bg-transparent px-1 py-[5px] text-[13px] leading-[18px] text-ink outline-none [overflow-wrap:anywhere] placeholder:text-ink-3 ${
              expanded ? "col-span-full col-start-1 row-start-1" : "col-start-2 row-start-1"
            }`}
          />

          {/* generation picker */}
          <button
            ref={pickerRef}
            type="button"
            aria-expanded={pickerOpen}
            aria-label={pickerLabels.temp}
            onClick={() => {
              setPlusOpen(false);
              setPickerOpen((current) => !current);
            }}
            className={`flex h-7 shrink-0 items-center gap-1 rounded-[8px] px-1.5 text-[12px] font-medium text-ink-2 transition-colors duration-150 hover:bg-hover hover:text-ink ${
              expanded ? "col-start-2 row-start-2 justify-self-end" : "col-start-3 row-start-1"
            }`}
          >
            {pickerLabels.temps[temperature]}
            <span className="text-ink-3">
              <Icon size={11} strokeWidth={2.4}><path d="M6 9l6 6 6-6" /></Icon>
            </span>
          </button>

          {/* send — tactile square */}
          <button
            type="button"
            aria-label="Send"
            disabled={!canSend}
            onClick={send}
            className={`flex size-7 shrink-0 items-center justify-center rounded-[8px] transition-[background-color,color,transform] duration-200 enabled:active:scale-[0.94] ${
              expanded ? "col-start-4 row-start-2" : "col-start-4 row-start-1"
            }`}
            style={{
              background: canSend ? "var(--accent)" : "var(--line-strong)",
              color: canSend ? "#fff" : "var(--ink-2)",
            }}
          >
            <Icon size={16} strokeWidth={2.4}><path d="M12 19V5M5 12l7-7 7 7" /></Icon>
          </button>
        </div>
      </div>
      {footerHint && (
        <div className="mt-1.5 flex items-center justify-end px-1 font-mono text-[10.5px] text-ink-3/70">
          <span>{footerHint}</span>
        </div>
      )}
    </div>
  );
}
