/*
 * SelectionActions — Beautiful UI (https://www.beautifului.dev/)
 * MIT License — Copyright (c) 2026 Shane Levine. See ../LICENSE.
 * Adapted for OpenStanley: select any text inside a OpenStanley reply → the
 * floating action bar attaches beneath the selection ("make it
 * shorter", "more Arabic", "punchier", or a custom instruction) →
 * the rewrite streams in via the backend, then Keep / Discard applies
 * it to the message in place.
 */
"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from "react";
import {
  ArrowUp,
  Check,
  NavArrowRight,
  Refresh,
  Xmark,
} from "iconoir-react";
import { Shimmer } from '../atoms/Shimmer';
import { StreamText } from '../atoms/StreamText';

/* ─────────────────────────────────────────────────────────
 * SELECTION ACTIONS
 * A contextual AI bar attached beneath selected text inside the
 * host element. The global theme owns its surface; this component
 * only composes surface, ink, accent, radius and motion tokens.
 * ───────────────────────────────────────────────────────── */

type Mode = "idle" | "thinking" | "streaming" | "result";

const iconProps = {
  width: 14,
  height: 14,
  strokeWidth: 1.8,
  "aria-hidden": true,
} as const;

const icons = {
  send: <ArrowUp width="16" height="16" strokeWidth="2.4" aria-hidden="true" />,
  chevron: <NavArrowRight {...iconProps} />,
  check: <Check {...iconProps} />,
  close: <Xmark {...iconProps} />,
  retry: <Refresh {...iconProps} />,
};

const control =
  "inline-flex h-7 shrink-0 items-center gap-1 rounded-full px-2.5 text-[12px] font-normal text-ink transition-[background-color,color,transform] duration-150 hover:bg-hover active:scale-[0.96]";

const primary =
  "inline-flex h-7 shrink-0 items-center gap-1 rounded-full bg-ink px-2.5 text-[12.5px] font-normal text-canvas shadow-hairline transition-[opacity,transform] duration-150 hover:opacity-90 active:scale-[0.96]";

export interface SelectionActionDef {
  key: string;
  label: string;
  /** instruction sent to the rewrite backend with the selection */
  instruction: string;
}

export default function SelectionActions({
  hostRef,
  enabled,
  actions,
  busyLabel,
  failedLabel,
  customPlaceholder,
  keepLabel,
  discardLabel,
  retryLabel,
  /** run the rewrite; resolves with the replacement text */
  onRewrite,
  /** apply the replacement to the source message */
  onApply,
}: {
  hostRef: RefObject<HTMLDivElement>;
  enabled: boolean;
  actions: SelectionActionDef[];
  busyLabel: string;
  failedLabel: string;
  customPlaceholder: string;
  keepLabel: string;
  discardLabel: string;
  retryLabel?: string;
  onRewrite: (instruction: string, selected: string) => Promise<string>;
  onApply: (selected: string, replacement: string) => void;
}) {
  const [mode, setMode] = useState<Mode>("idle");
  const [action, setAction] = useState<string>("");
  const [selected, setSelected] = useState("");
  const [rewritten, setRewritten] = useState("");
  const [error, setError] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [typingWidth, setTypingWidth] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [anchor, setAnchor] = useState({ x: 0, y: 0 });
  const [positioned, setPositioned] = useState(false);

  const barRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<number | null>(null);
  const previousModeRef = useRef<Mode>("idle");
  const lastWidthRef = useRef(0);
  const widthAnimationRef = useRef<Animation | null>(null);

  /* Place the bar beneath the final selected line, centered on the
   * selection bounds. Batches streaming reflow into one rAF. */
  const place = useCallback(() => {
    if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    frameRef.current = requestAnimationFrame(() => {
      const host = hostRef.current;
      if (!host) return;
      const sel = window.getSelection();
      if (!sel || sel.rangeCount === 0 || sel.isCollapsed || !sel.toString().trim()) {
        setPositioned(false);
        return;
      }
      const range = sel.getRangeAt(0);
      if (!host.contains(range.commonAncestorContainer)) {
        setPositioned(false);
        return;
      }
      const bounds = range.getBoundingClientRect();
      const lines = Array.from(range.getClientRects());
      const lastLine = lines.at(-1);
      if (!lastLine) return;

      const hostBounds = host.getBoundingClientRect();
      const next = {
        x: Math.round(bounds.left - hostBounds.left + bounds.width / 2),
        y: Math.round(lastLine.bottom - hostBounds.top + 8),
      };
      setAnchor((current) =>
        current.x === next.x && current.y === next.y ? current : next,
      );
      setPositioned(true);
    });
  }, [hostRef]);

  /* track selection inside the host; clear the bar when it collapses */
  useEffect(() => {
    if (!enabled) return;
    const onSelChange = (): void => {
      const sel = window.getSelection();
      const text = sel ? sel.toString().trim() : "";
      if (!text || !hostRef.current) return;
      const selAnchor = sel?.anchorNode;
      if (!selAnchor || !hostRef.current.contains(selAnchor)) return;
      if (mode === "idle") setSelected(text);
      place();
    };
    document.addEventListener("selectionchange", onSelChange);
    window.addEventListener("resize", place);
    return () => {
      document.removeEventListener("selectionchange", onSelChange);
      window.removeEventListener("resize", place);
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    };
  }, [enabled, hostRef, mode, place]);

  useLayoutEffect(() => {
    place();
  }, [mode, place]);

  /* Intrinsic width handles the preset expansion. When the entire content
   * changes between idle, loading and confirmation, animate from the last
   * rendered width to the new intrinsic width before the browser paints. */
  useLayoutEffect(() => {
    const bar = barRef.current;
    const content = contentRef.current;
    if (!bar || !content) return;

    const nextWidth = Math.ceil(content.getBoundingClientRect().width) + 8;
    const previousWidth =
      lastWidthRef.current || Math.ceil(bar.getBoundingClientRect().width);

    if (
      previousModeRef.current !== mode &&
      Math.abs(nextWidth - previousWidth) > 1
    ) {
      widthAnimationRef.current?.cancel();
      const animation = bar.animate(
        [
          { width: `${previousWidth}px` },
          { width: `${nextWidth}px` },
        ],
        {
          duration: 320,
          easing: "cubic-bezier(0.23,1,0.32,1)",
        },
      );
      widthAnimationRef.current = animation;
      animation.onfinish = () => {
        lastWidthRef.current = nextWidth;
        widthAnimationRef.current = null;
      };
    } else {
      lastWidthRef.current = nextWidth;
    }

    previousModeRef.current = mode;
  }, [mode]);

  useEffect(() => {
    const content = contentRef.current;
    if (!content) return;

    const observer = new ResizeObserver(() => {
      if (widthAnimationRef.current?.playState === "running") return;
      lastWidthRef.current =
        Math.ceil(content.getBoundingClientRect().width) + 8;
    });
    observer.observe(content);
    return () => {
      observer.disconnect();
      widthAnimationRef.current?.cancel();
    };
  }, []);

  const run = (label: string, instruction: string): void => {
    const sel = window.getSelection()?.toString().trim() || selected;
    if (!sel) return;
    setSelected(sel);
    setAction(label);
    setExpanded(false);
    setPrompt("");
    setTypingWidth(null);
    setError(false);
    setMode("thinking");
    onRewrite(instruction, sel)
      .then((text) => {
        setRewritten(text.trim());
        setMode("streaming");
      })
      .catch(() => {
        setError(true);
        setMode("idle");
      });
  };

  const reset = (): void => {
    setExpanded(false);
    setPrompt("");
    setTypingWidth(null);
    setMode("idle");
    setPositioned(false);
  };

  const busy = mode === "thinking" || mode === "streaming";
  const visible = enabled && positioned && selected.length > 0;
  const hasPrompt = prompt.trim().length > 0;
  const baseActions = expanded ? actions : actions.slice(0, 2);

  return (
    <div
      aria-hidden={!visible}
      className="pointer-events-none absolute top-0 left-0 z-10"
      style={{
        transform: `translate3d(${anchor.x}px, ${anchor.y}px, 0) translateX(-50%)`,
        transition:
          "transform 320ms cubic-bezier(0.77,0,0.175,1), opacity 180ms ease-out",
        opacity: visible ? 1 : 0,
        pointerEvents: visible ? "auto" : "none",
        willChange: "transform",
      }}
    >
      {/* the pill only mounts while a selection is active — hidden bars
          must not sit in the a11y tree / tab order of every message */}
      {visible && (
      /* A 36px pill wraps 28px controls at a 4px inset. */
      <div
        ref={barRef}
        dir="ltr"
        className="flex h-9 w-fit max-w-[calc(100vw-48px)] items-center justify-center gap-0.5 overflow-hidden rounded-full bg-surface p-1 font-sans font-normal text-ink antialiased shadow-overlay"
        style={{
          width:
            mode === "idle" && hasPrompt && typingWidth
              ? typingWidth
              : undefined,
          animation: "pop-in 220ms cubic-bezier(0.23,1,0.32,1) both",
        }}
      >
        <div
          ref={contentRef}
          className="flex w-fit shrink-0 items-center justify-center gap-0.5"
          style={{
            width:
              mode === "idle" && hasPrompt && typingWidth
                ? typingWidth - 8
                : undefined,
          }}
        >
          {busy && (
            <span className="inline-flex h-7 items-center gap-1.5 whitespace-nowrap px-2.5 text-[12.5px] font-normal text-ink-2">
              <span
                className="size-3 shrink-0 rounded-full border-[1.5px] border-line-strong border-t-ink-2"
                style={{ animation: "spin 700ms linear infinite" }}
              />
              {mode === "thinking" ? (
                <Shimmer className="text-[12.5px] font-normal">
                  {busyLabel}…
                </Shimmer>
              ) : (
                <StreamText
                  text={rewritten}
                  onProgress={place}
                  onDone={() => setMode("result")}
                />
              )}
            </span>
          )}

          {mode === "idle" && error && (
            <span className="inline-flex h-7 items-center whitespace-nowrap px-2.5 text-[12px] text-red">
              {failedLabel}
            </span>
          )}

          {mode === "result" && (
            <>
              <button
                type="button"
                onClick={() => {
                  onApply(selected, rewritten);
                  reset();
                }}
                className={primary}
              >
                {icons.check}
                {keepLabel}
              </button>
              <button type="button" onClick={reset} className={control}>
                {icons.close}
                {discardLabel}
              </button>
              <span className="mx-0.5 h-4 w-px shrink-0 bg-line" />
              <button
                type="button"
                aria-label={retryLabel ?? "Try again"}
                onClick={() => run(action, action)}
                className="flex size-7 shrink-0 items-center justify-center rounded-full text-ink-3 transition-[background-color,color,transform] duration-150 hover:bg-hover-2 hover:text-ink-2 active:scale-[0.96]"
              >
                {icons.retry}
              </button>
            </>
          )}

          {mode === "idle" && !error && (
            <>
              <div
                className="flex min-w-0 items-center overflow-hidden transition-[max-width,opacity,transform] duration-400"
                style={{
                  maxWidth: expanded
                    ? 0
                    : hasPrompt && typingWidth
                      ? typingWidth - 40
                      : 145,
                  opacity: expanded ? 0 : 1,
                  transform: expanded ? "translateX(-8px)" : "translateX(0)",
                  transitionTimingFunction: "cubic-bezier(0.23,1,0.32,1)",
                }}
              >
                <form
                  className="flex h-7 shrink-0 items-center transition-[width] duration-400"
                  style={{
                    width: hasPrompt && typingWidth ? typingWidth - 40 : 145,
                    transitionTimingFunction: "cubic-bezier(0.23,1,0.32,1)",
                  }}
                  onSubmit={(event) => {
                    event.preventDefault();
                    run(prompt.trim() || "Improve", prompt.trim());
                  }}
                >
                  <input
                    value={prompt}
                    onChange={(event) => {
                      const next = event.target.value;
                      if (!prompt.trim() && next.trim()) {
                        setTypingWidth(
                          Math.ceil(
                            barRef.current?.getBoundingClientRect().width ?? 0,
                          ),
                        );
                      } else if (!next.trim()) {
                        setTypingWidth(null);
                      }
                      setPrompt(next);
                    }}
                    aria-label={customPlaceholder}
                    placeholder={customPlaceholder}
                    className="h-7 w-full bg-transparent pr-2.5 pl-3 text-[12.5px] text-ink placeholder:text-ink-3"
                  />
                </form>
              </div>

              <div
                className="flex min-w-0 items-center gap-0.5 overflow-hidden transition-[max-width,opacity,transform] duration-400"
                style={{
                  maxWidth: hasPrompt ? 0 : expanded ? 462 : 224,
                  opacity: hasPrompt ? 0 : 1,
                  transform: hasPrompt ? "translateX(-8px)" : "translateX(0)",
                  transitionTimingFunction: "cubic-bezier(0.23,1,0.32,1)",
                }}
              >
                {!expanded && (
                  <span className="mx-1 h-4 w-px shrink-0 bg-line-strong" />
                )}
                {baseActions.map((a) => (
                  <button
                    key={a.key}
                    type="button"
                    onClick={() => run(a.label, a.instruction)}
                    className={control}
                  >
                    {a.label}
                  </button>
                ))}

                <span className="mx-0.5 h-4 w-px shrink-0 bg-line" />
                <button
                  type="button"
                  aria-label={expanded ? "Show fewer actions" : "Show more actions"}
                  aria-expanded={expanded}
                  onClick={() => setExpanded((value) => !value)}
                  className="flex size-7 shrink-0 items-center justify-center rounded-full text-ink transition-[background-color,transform] duration-200 hover:bg-hover active:scale-[0.96]"
                >
                  <span
                    className="flex transition-transform duration-400"
                    style={{
                      transform: expanded ? "rotate(180deg)" : "rotate(0deg)",
                      transitionTimingFunction: "cubic-bezier(0.23,1,0.32,1)",
                    }}
                  >
                    {icons.chevron}
                  </span>
                </button>
              </div>

              <div
                className="flex min-w-0 items-center overflow-hidden transition-[max-width,opacity,transform] duration-400"
                style={{
                  maxWidth: hasPrompt ? 30 : 0,
                  opacity: hasPrompt ? 1 : 0,
                  transform: hasPrompt ? "scale(1)" : "scale(0.88)",
                  transitionTimingFunction: "cubic-bezier(0.23,1,0.32,1)",
                }}
              >
                <button
                  type="button"
                  aria-label="Send edit instruction"
                  onClick={() => run(prompt.trim(), prompt.trim())}
                  className="flex size-7 shrink-0 items-center justify-center rounded-full bg-ink text-surface transition-[opacity,transform] duration-200 active:scale-[0.94]"
                >
                  {icons.send}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
      )}
    </div>
  );
}
