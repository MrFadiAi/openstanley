/*
 * StreamText — progressive text reveal atom (reconstructed support atom).
 * Beautiful UI (https://www.beautifului.dev/) — MIT License, © 2026 Shane Levine.
 * The original atom is not published; this matches its observed API:
 *   <StreamText text={...} onProgress={() => void} onDone={() => void} />
 * Reveals `text` chunk by chunk (streaming feel), notifying onProgress after
 * each chunk so callers can re-measure layout, and onDone at the end.
 */

"use client";

import { useEffect, useState } from "react";

const CHUNK = 3; // characters per tick
const TICK_MS = 24;

export function StreamText({
  text,
  onProgress,
  onDone,
  className = "",
}: {
  text: string;
  onProgress?: () => void;
  onDone?: () => void;
  className?: string;
}) {
  const [shown, setShown] = useState(0);

  useEffect(() => {
    setShown(0);
    if (!text) {
      onDone?.();
      return;
    }
    let i = 0;
    const t = window.setInterval(() => {
      i = Math.min(text.length, i + CHUNK);
      setShown(i);
      onProgress?.();
      if (i >= text.length) {
        window.clearInterval(t);
        onDone?.();
      }
    }, TICK_MS);
    return () => window.clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  return <span className={className}>{text.slice(0, shown)}</span>;
}
