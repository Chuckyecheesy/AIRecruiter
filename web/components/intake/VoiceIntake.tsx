"use client";

import { useState, type FC } from "react";
import { briefFileName, downloadMarkdown, toBriefFreeText, toBriefMarkdown } from "@/lib/voice/brief";
import { voiceConfig } from "@/lib/voice/config";
import { ElevenLabsVoiceProvider, useElevenLabsIntakeVoice } from "@/lib/voice/elevenlabs";
import { useMockIntakeVoice } from "@/lib/voice/mock";
import type { IntakeVoice, RoleDraft, VoiceStatus } from "@/lib/voice/types";
import { cn } from "@/lib/utils";
import { BriefDocument } from "./BriefDocument";
import { VoiceOrb } from "./VoiceOrb";

export type VoiceIntakeProps = {
  busy: boolean;
  err: string | null;
  /** Create the job from the captured brief. `freeText` is the markdown brief. */
  onSubmit: (draft: RoleDraft, freeText: string) => void;
  /** Switch to the typed form, prefilled with what was captured. */
  onEdit: (draft: RoleDraft) => void;
};

export const VoiceIntake: FC<VoiceIntakeProps> = (props) =>
  voiceConfig().mode === "elevenlabs" ? (
    <ElevenLabsVoiceProvider>
      <LiveVoiceIntake {...props} />
    </ElevenLabsVoiceProvider>
  ) : (
    <MockVoiceIntake {...props} />
  );

const LiveVoiceIntake: FC<VoiceIntakeProps> = (props) => <VoiceIntakeView voice={useElevenLabsIntakeVoice()} {...props} />;
const MockVoiceIntake: FC<VoiceIntakeProps> = (props) => <VoiceIntakeView voice={useMockIntakeVoice()} {...props} />;

const STATUS_COPY: Record<VoiceStatus, string> = {
  idle: "Tap to start talking",
  connecting: "Connecting…",
  listening: "Listening…",
  speaking: "Recruiter is speaking",
  ended: "Got it. Check the brief.",
  error: "Couldn’t connect",
};

const VoiceIntakeView: FC<VoiceIntakeProps & { voice: IntakeVoice }> = ({ voice, busy, err, onSubmit, onEdit }) => {
  const { status, transcript, draft, lastUpdated } = voice;
  const [dateLabel, setDateLabel] = useState<string>();
  const [titleOverride, setTitleOverride] = useState("");
  const live = status === "listening" || status === "speaking" || status === "connecting";
  const ended = status === "ended";
  const finalDraft: RoleDraft = { ...draft, title: draft.title || titleOverride.trim() };
  const canSubmit = ended && !!finalDraft.title && !busy;

  const toggle = () => {
    if (live) return voice.stop();
    setTitleOverride("");
    setDateLabel(new Date().toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }));
    voice.start();
  };

  const recent = transcript.slice(-4);

  return (
    <div className="grid items-start gap-10 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] lg:gap-14">
      {/* call */}
      <section aria-label="Voice call" className="flex flex-col items-center pt-2 text-center">
        <VoiceOrb
          status={status}
          inputLevel={voice.inputLevel}
          outputLevel={voice.outputLevel}
          onClick={toggle}
          label={live ? "End call" : ended ? "Talk again" : "Start talking"}
        />
        <p aria-live="polite" className="font-display mt-4 text-xl font-medium text-ink">
          {STATUS_COPY[status]}
        </p>
        <p className="label-mono mt-1.5 text-quiet">
          {live ? "Tap the orb to finish" : ended ? "Tap to talk again" : voice.mode === "mock" ? "Demo call · scripted" : "Uses your microphone"}
        </p>
        {voice.error && <p className="mt-3 max-w-sm text-sm text-danger">{voice.error}</p>}

        <ol
          aria-label="Transcript"
          className="mt-8 grid w-full max-w-md gap-3 text-left [mask-image:linear-gradient(to_bottom,transparent,black_38%)]"
        >
          {recent.map((line, i) => (
            <li key={`${transcript.length - recent.length + i}`} className="animate-rise">
              <span className={cn("label-mono", line.role === "ceo" ? "text-gate" : "text-agent")}>
                {line.role === "ceo" ? "You" : "Recruiter"}
              </span>
              <p className={cn("mt-0.5 text-[15px] leading-snug", line.role === "ceo" ? "text-ink" : "text-quiet")}>{line.text}</p>
            </li>
          ))}
        </ol>
      </section>

      {/* brief */}
      <section aria-label="Role brief">
        <BriefDocument
          draft={finalDraft}
          live={live}
          lastUpdated={lastUpdated}
          dateLabel={dateLabel}
          footer={
            ended ? (
              <div className="grid gap-4">
                {!draft.title && (
                  <label className="grid gap-1.5">
                    <span className="label-mono text-gate">Name the role to continue</span>
                    <input
                      autoFocus
                      value={titleOverride}
                      onChange={(e) => setTitleOverride(e.target.value)}
                      placeholder="e.g. Founding AI Engineer"
                      className="w-full rounded-xl border border-line bg-canvas px-3 py-2 text-[15px] text-ink outline-none focus:border-ink"
                    />
                  </label>
                )}
                {err && <p className="text-sm text-danger">{err}</p>}
                <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
                  <button
                    type="button"
                    disabled={!canSubmit}
                    onClick={() => onSubmit(finalDraft, toBriefFreeText(finalDraft, transcript))}
                    className="rounded-full bg-gate px-5 py-2.5 font-medium text-surface transition hover:bg-gate-deep disabled:opacity-50"
                  >
                    {busy ? "Writing the rubric…" : "Generate rubric →"}
                  </button>
                  <button
                    type="button"
                    onClick={() => downloadMarkdown(briefFileName(finalDraft), toBriefMarkdown(finalDraft, transcript, "voice"))}
                    className="rounded-full border border-line px-4 py-2.5 text-sm text-ink transition hover:border-ink/40"
                  >
                    Download brief.md
                  </button>
                  <button type="button" onClick={() => onEdit(finalDraft)} className="text-sm text-quiet underline-offset-4 hover:text-ink hover:underline">
                    Edit in form
                  </button>
                  <button type="button" onClick={voice.reset} className="text-sm text-quiet underline-offset-4 hover:text-ink hover:underline">
                    Start over
                  </button>
                </div>
              </div>
            ) : status === "idle" ? (
              <p className="text-sm text-quiet">This fills in as you talk. You’ll get it as a markdown file at the end.</p>
            ) : null
          }
        />
      </section>
    </div>
  );
};
