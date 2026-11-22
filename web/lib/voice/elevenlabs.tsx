"use client";

import { ConversationProvider, useConversation, type MessagePayload } from "@elevenlabs/react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { voiceConfig } from "./config";
import { applyBriefUpdate, EMPTY_DRAFT, type BriefUpdate, type IntakeVoice, type RoleDraft, type TranscriptLine, type VoiceStatus } from "./types";

/** Wrap the intake UI with this when running against a real ElevenLabs agent. */
export function ElevenLabsVoiceProvider({ children }: { children: ReactNode }) {
  return <ConversationProvider>{children}</ConversationProvider>;
}

type SessionAuth = { agentId: string } | { conversationToken: string } | { signedUrl: string };

/**
 * Resolve how to open the session. With a token URL (a server route the teammate owns that holds the API key),
 * the browser never sees the key. Without it, a public agent ID is used directly.
 * Token URL contract: GET → `{ "token": "..." }` (WebRTC) or `{ "signedUrl": "wss://..." }` (WebSocket).
 */
async function resolveAuth(): Promise<SessionAuth> {
  const { tokenUrl, agentId } = voiceConfig();
  if (tokenUrl) {
    const res = await fetch(tokenUrl, { cache: "no-store" });
    if (!res.ok) throw new Error(`Voice token request failed (${res.status})`);
    const body = (await res.json()) as { token?: string; conversationToken?: string; signedUrl?: string; signed_url?: string };
    const token = body.token ?? body.conversationToken;
    if (token) return { conversationToken: token };
    const signedUrl = body.signedUrl ?? body.signed_url;
    if (signedUrl) return { signedUrl };
    throw new Error("Voice token response had no token or signedUrl");
  }
  if (agentId) return { agentId };
  throw new Error("No ElevenLabs agent configured");
}

/** Must be rendered inside `ElevenLabsVoiceProvider`. */
export function useElevenLabsIntakeVoice(): IntakeVoice {
  const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
  const [draft, setDraft] = useState<RoleDraft>(EMPTY_DRAFT);
  const [lastUpdated, setLastUpdated] = useState<(keyof RoleDraft)[]>([]);
  const [phase, setPhase] = useState<"idle" | "starting" | "live" | "ended">("idle");
  const [error, setError] = useState<string | null>(null);
  const draftRef = useRef<RoleDraft>(EMPTY_DRAFT);
  const endRef = useRef<() => void>(() => {});

  const clientTools = useMemo(
    () => ({
      update_role_brief: (params: Record<string, unknown>) => {
        const { draft: next, changed } = applyBriefUpdate(draftRef.current, params as BriefUpdate);
        draftRef.current = next;
        setDraft(next);
        setLastUpdated(changed);
        return changed.length ? `Brief updated: ${changed.join(", ")}` : "No change to the brief";
      },
      finish_intake: () => {
        // let the agent finish its closing sentence before hanging up
        setTimeout(() => endRef.current(), 2500);
        return "Ending the call. The CEO will review the brief on screen.";
      },
    }),
    [],
  );

  const onMessage = useCallback((m: MessagePayload) => {
    if (!m.message?.trim()) return;
    setTranscript((xs) => [...xs, { role: m.role === "user" ? "ceo" : "agent", text: m.message, at: new Date().toISOString() }]);
  }, []);

  const conversation = useConversation({
    clientTools,
    onMessage,
    onConnect: () => setPhase("live"),
    onDisconnect: () => setPhase((p) => (p === "idle" ? p : "ended")),
    onError: (message: string) => setError(message || "Voice connection error"),
  });
  const { endSession } = conversation;
  useEffect(() => {
    endRef.current = endSession;
  }, [endSession]);

  const start = useCallback(async () => {
    setError(null);
    setTranscript([]);
    draftRef.current = EMPTY_DRAFT;
    setDraft(EMPTY_DRAFT);
    setLastUpdated([]);
    setPhase("starting");
    try {
      await navigator.mediaDevices.getUserMedia({ audio: true });
      const auth = await resolveAuth();
      conversation.startSession({ ...auth, dynamicVariables: { today: new Date().toISOString().slice(0, 10) } });
    } catch (e) {
      setPhase("idle");
      const msg = e instanceof Error ? e.message : String(e);
      setError(/denied|NotAllowed/i.test(msg) ? "Microphone access was blocked. Allow it in the browser and try again." : msg);
    }
  }, [conversation]);

  const stop = useCallback(() => {
    conversation.endSession();
    setPhase("ended");
  }, [conversation]);

  const reset = useCallback(() => {
    if (conversation.status === "connected") conversation.endSession();
    setPhase("idle");
    setError(null);
    setTranscript([]);
    draftRef.current = EMPTY_DRAFT;
    setDraft(EMPTY_DRAFT);
    setLastUpdated([]);
  }, [conversation]);

  let status: VoiceStatus;
  if (error && phase !== "live") status = "error";
  else if (phase === "starting" || conversation.status === "connecting") status = "connecting";
  else if (phase === "live" && conversation.status === "connected") status = conversation.isSpeaking ? "speaking" : "listening";
  else if (phase === "ended" || (phase === "live" && conversation.status === "disconnected")) status = "ended";
  else status = "idle";

  return {
    mode: "elevenlabs",
    status,
    error,
    transcript,
    draft,
    lastUpdated,
    start: () => void start(),
    stop,
    reset,
    inputLevel: conversation.getInputVolume,
    outputLevel: conversation.getOutputVolume,
  };
}
