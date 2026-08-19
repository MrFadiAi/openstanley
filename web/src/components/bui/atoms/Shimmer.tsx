/*
 * Shimmer — shimmering text atom (reconstructed support atom).
 * Beautiful UI (https://www.beautifului.dev/) — MIT License, © 2026 Shane Levine.
 * The original atom is not published; this matches its observed API:
 *   <Shimmer className="...">{children}</Shimmer>
 */

"use client";

import type { CSSProperties, ReactNode } from "react";

export function Shimmer({
  children,
  className = "",
  style,
}: {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <span
      className={`bg-clip-text text-transparent ${className}`}
      style={{
        backgroundImage:
          "linear-gradient(90deg, var(--ink-3) 35%, var(--ink) 50%, var(--ink-3) 65%)",
        backgroundSize: "200% 100%",
        animation: "shimmer-text 1.4s linear infinite",
        ...style,
      }}
    >
      {children}
    </span>
  );
}
