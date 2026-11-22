"use client";

import { useEffect, useRef, type FC } from "react";
import type { VoiceStatus } from "@/lib/voice/types";
import { cn } from "@/lib/utils";

type Props = {
  status: VoiceStatus;
  inputLevel: () => number;
  outputLevel: () => number;
  onClick: () => void;
  label: string;
};

const RINGS = [0, 1, 2];

/**
 * The orb is the call button. Terracotta rings follow the CEO's voice; the core turns teal while the recruiter speaks.
 * Levels are read every animation frame and written straight to the DOM, so the page doesn't re-render at 60fps.
 */
export const VoiceOrb: FC<Props> = ({ status, inputLevel, outputLevel, onClick, label }) => {
  const ringRefs = useRef<(HTMLSpanElement | null)[]>([]);
  const coreRef = useRef<HTMLSpanElement | null>(null);
  const live = status === "listening" || status === "speaking";

  useEffect(() => {
    if (!live) {
      ringRefs.current.forEach((r) => r && (r.style.transform = ""));
      if (coreRef.current) coreRef.current.style.transform = "";
      return;
    }
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced) return;
    let raf = 0;
    let smooth = 0;
    const tick = () => {
      const lvl = Math.min(1, Math.max(inputLevel() || 0, outputLevel() || 0));
      smooth += (lvl - smooth) * 0.18;
      ringRefs.current.forEach((r, i) => {
        if (!r) return;
        const s = 1 + smooth * (0.18 + i * 0.16);
        r.style.transform = `scale(${s.toFixed(3)})`;
        r.style.opacity = String(Math.max(0.12, 0.55 - i * 0.16 + smooth * 0.25));
      });
      if (coreRef.current) coreRef.current.style.transform = `scale(${(1 + smooth * 0.06).toFixed(3)})`;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [live, inputLevel, outputLevel]);

  const speaking = status === "speaking";

  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      disabled={status === "connecting"}
      className="group relative grid size-56 place-items-center rounded-full outline-none focus-visible:ring-2 focus-visible:ring-gate focus-visible:ring-offset-4 focus-visible:ring-offset-canvas disabled:cursor-wait"
    >
      {RINGS.map((i) => (
        <span
          key={i}
          ref={(el) => {
            ringRefs.current[i] = el;
          }}
          aria-hidden
          className={cn(
            "absolute rounded-full border transition-[border-color,opacity] duration-500",
            speaking ? "border-agent/50" : "border-gate/45",
            !live && "animate-[orb-breathe_4.5s_ease-in-out_infinite] opacity-40",
          )}
          style={{ inset: `${28 - i * 14}px`, animationDelay: `${i * 0.45}s` }}
        />
      ))}
      <span
        ref={coreRef}
        aria-hidden
        className={cn(
          "absolute inset-[42px] rounded-full transition-[background,box-shadow] duration-700",
          speaking
            ? "bg-[radial-gradient(circle_at_35%_30%,#4aa59d,#1f6f6b_70%)] shadow-[0_18px_50px_-12px_rgba(31,111,107,0.55)]"
            : "bg-[radial-gradient(circle_at_35%_30%,#e58a5f,#c2562e_68%,#a3441f)] shadow-[0_18px_50px_-12px_rgba(194,86,46,0.55)]",
          status === "connecting" && "animate-pulse",
          "group-hover:brightness-105",
        )}
      />
      <span aria-hidden className="relative z-10 text-surface">
        {live ? <StopGlyph /> : <MicGlyph />}
      </span>
    </button>
  );
};

const MicGlyph = () => (
  <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
    <rect x="9" y="3" width="6" height="11" rx="3" />
    <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" />
  </svg>
);

const StopGlyph = () => (
  <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor">
    <rect x="6" y="6" width="12" height="12" rx="2.5" />
  </svg>
);
