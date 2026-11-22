"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { applyBriefUpdate, EMPTY_DRAFT, type BriefUpdate, type IntakeVoice, type RoleDraft, type TranscriptLine, type VoiceStatus } from "./types";

type Step = { role: "ceo" | "agent"; text: string; update?: BriefUpdate };

/** A scripted intake call. Used when no ElevenLabs agent is configured, so the page works offline. */
const SCRIPT: Step[] = [
  { role: "agent", text: "Hi! I'm your recruiter. Who are we hiring?" },
  {
    role: "ceo",
    text: "We need an AI engineer who can own our retrieval stack. Mostly RAG work, in TypeScript.",
    update: { title: "AI Engineer (RAG + TypeScript)", must_haves: ["RAG", "TypeScript"] },
  },
  { role: "agent", text: "Got it. How do you want to judge whether their RAG work is any good?" },
  {
    role: "ceo",
    text: "They have to have shipped LLM evals. Real ones, not vibes. React and some Python would be a bonus.",
    update: { must_haves: ["LLM evaluation"], nice_to_haves: ["React", "Python"] },
  },
  { role: "agent", text: "Where should they be based, and what level are you thinking?" },
  {
    role: "ceo",
    text: "Toronto or remote is fine. Mid-level, someone who can work without hand-holding.",
    update: { location: "Toronto / remote", seniority: "mid", notes: "Should work independently without hand-holding." },
  },
  { role: "agent", text: "Anything that would rule someone out?" },
  {
    role: "ceo",
    text: "Startup pace matters. If they've only worked at big companies, I'd want to hear how they'd handle ambiguity.",
    update: { notes: "Startup pace matters; probe how big-company candidates handle ambiguity." },
  },
  { role: "agent", text: "Perfect. I've written that up as a brief. Have a look and I'll start sourcing when you're ready." },
];

const msPerChar = 38;

export function useMockIntakeVoice(): IntakeVoice {
  const [status, setStatus] = useState<VoiceStatus>("idle");
  const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
  const [draft, setDraft] = useState<RoleDraft>(EMPTY_DRAFT);
  const [lastUpdated, setLastUpdated] = useState<(keyof RoleDraft)[]>([]);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  const statusRef = useRef<VoiceStatus>("idle");
  const draftRef = useRef<RoleDraft>(EMPTY_DRAFT);
  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  const clear = () => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  };
  useEffect(() => clear, []);

  const start = useCallback(() => {
    clear();
    setTranscript([]);
    draftRef.current = EMPTY_DRAFT;
    setDraft(EMPTY_DRAFT);
    setLastUpdated([]);
    setStatus("connecting");
    let t = 900;
    SCRIPT.forEach((step) => {
      const speakFor = Math.max(1400, step.text.length * msPerChar);
      timers.current.push(
        setTimeout(() => {
          setStatus(step.role === "agent" ? "speaking" : "listening");
          setTranscript((xs) => [...xs, { role: step.role, text: step.text, at: new Date().toISOString() }]);
        }, t),
      );
      if (step.update) {
        const u = step.update;
        timers.current.push(
          setTimeout(() => {
            const { draft: next, changed } = applyBriefUpdate(draftRef.current, u);
            draftRef.current = next;
            setDraft(next);
            setLastUpdated(changed);
          }, t + speakFor * 0.7),
        );
      }
      t += speakFor + 500;
    });
    timers.current.push(setTimeout(() => setStatus("ended"), t));
  }, []);

  const stop = useCallback(() => {
    clear();
    setStatus((s) => (s === "idle" ? s : "ended"));
  }, []);

  const reset = useCallback(() => {
    clear();
    setStatus("idle");
    setTranscript([]);
    draftRef.current = EMPTY_DRAFT;
    setDraft(EMPTY_DRAFT);
    setLastUpdated([]);
  }, []);

  const wave = (seed: number) => {
    const t = performance.now() / 1000;
    return Math.max(0, 0.35 + 0.3 * Math.sin(t * 7 + seed) * Math.sin(t * 2.3 + seed * 2) + 0.15 * Math.sin(t * 13));
  };
  const inputLevel = useCallback(() => (statusRef.current === "listening" ? wave(1) : 0), []);
  const outputLevel = useCallback(() => (statusRef.current === "speaking" ? wave(4) : 0), []);

  return { mode: "mock", status, error: null, transcript, draft, lastUpdated, start, stop, reset, inputLevel, outputLevel };
}
