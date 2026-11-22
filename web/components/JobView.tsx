"use client";

import { AssistantRuntimeProvider } from "@assistant-ui/react";
import Link from "next/link";
import { useEffect, useState, type FC } from "react";
import { Thread } from "@/components/assistant-ui/thread.aui";
import { api } from "@/lib/api";
import { useRecruiterRuntime } from "@/lib/runtime";
import { useJobStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import { CandidateDrawer } from "./CandidateDrawer";
import { Pipeline } from "./Pipeline";
import { PolicyToggle } from "./PolicyToggle";
import { RecruiterToolUIs } from "./tool-uis";

const ACTION_NOUN: Record<string, string> = {
  send_outreach: "outreach email",
  send_followup: "follow-up",
  schedule_interview: "interview",
  reject_candidate_proposal: "rejection",
};

const Welcome: FC = () => (
  <div className="mb-6 px-2">
    <p className="text-xl font-medium text-ink">The agent hasn’t done anything yet.</p>
    <p className="mt-1 text-sm text-quiet">Its decisions, and every action that needs your approval, will show up here.</p>
  </div>
);

export const JobView: FC<{ jobId: string }> = ({ jobId }) => {
  const connect = useJobStore((s) => s.connect);
  useEffect(() => connect(jobId), [jobId, connect]);

  const runtime = useRecruiterRuntime();
  const job = useJobStore((s) => s.job);
  const error = useJobStore((s) => s.error);
  const streamStatus = useJobStore((s) => s.streamStatus);
  const candidateFilter = useJobStore((s) => s.candidateFilter);
  const setFilter = useJobStore((s) => s.setCandidateFilter);
  const filtered = useJobStore((s) => s.candidates.find((c) => c.id === s.candidateFilter));
  const focused = useJobStore((s) => (s.focusedApprovalId ? s.approvals[s.focusedApprovalId] : null));
  const pendingCount = useJobStore((s) => Object.values(s.approvals).filter((a) => a.status === "pending").length);
  const latestPending = useJobStore((s) => {
    const p = Object.values(s.approvals).filter(
      (a) => a.status === "pending" && (!s.candidateFilter || a.candidateId === s.candidateFilter),
    );
    return p.sort((a, b) => b.createdAt.localeCompare(a.createdAt))[0] ?? null;
  });
  const setFocus = useJobStore((s) => s.setFocusedApproval);
  const target = focused ?? latestPending;

  const placeholder = target
    ? `Feedback on ${target.candidateName}’s ${ACTION_NOUN[target.action]}, or “approve”`
    : "Ask for “status”, or say “pause”";

  if (error && !job)
    return (
      <main className="mx-auto max-w-lg p-8">
        <p className="text-ink">Couldn’t load this role: {error}</p>
        <p className="mt-2 text-sm text-quiet">Check that the API is running and NEXT_PUBLIC_API_URL points at it.</p>
        <Link href="/" className="mt-4 inline-block text-agent underline">
          Create a new role
        </Link>
      </main>
    );

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <RecruiterToolUIs />
      <div className="flex h-dvh flex-col">
        <header className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-line bg-white px-4 py-2.5">
          <div className="min-w-0">
            <Link href="/" className="text-xs text-quiet hover:text-ink">
              AIRecruiter
            </Link>
            <h1 className="truncate text-lg leading-tight font-semibold text-ink">{job?.title ?? "Loading…"}</h1>
          </div>
          {job && <JobStateControl />}
          <div className="ml-auto flex flex-wrap items-center gap-x-5 gap-y-2">
            {pendingCount > 0 && (
              <span className="rounded-full bg-gate/10 px-2.5 py-0.5 text-sm font-medium text-gate">
                {pendingCount} waiting for you
              </span>
            )}
            <PolicyToggle />
            <Link href={`/jobs/${jobId}/shortlist`} className="rounded-md border border-line px-3 py-1.5 text-sm text-ink hover:bg-canvas">
              Shortlist
            </Link>
            <span
              className={cn("size-2 rounded-full", streamStatus === "live" ? "bg-ok" : streamStatus === "polling" ? "bg-gate" : "bg-line")}
              title={streamStatus === "live" ? "Live" : streamStatus === "polling" ? "Live updates via polling" : "Connecting"}
            />
          </div>
        </header>

        <div className="flex min-h-0 flex-1 flex-col md:flex-row">
          <aside className="max-h-64 shrink-0 overflow-y-auto border-b border-line bg-canvas md:max-h-none md:w-72 md:border-r md:border-b-0" aria-label="Pipeline">
            <Pipeline />
          </aside>
          <main className="flex min-h-0 min-w-0 flex-1 flex-col bg-white">
            {(candidateFilter || target) && (
              <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-1.5 text-sm">
                {candidateFilter && (
                  <span className="flex items-center gap-1.5 rounded-full bg-canvas px-2.5 py-0.5">
                    Showing only {filtered?.name ?? "one candidate"}
                    <button type="button" onClick={() => setFilter(null)} className="text-quiet hover:text-ink" aria-label="Show everyone">
                      ✕
                    </button>
                  </span>
                )}
                {target && (
                  <span className="flex items-center gap-1.5 text-quiet">
                    Your next message applies to{" "}
                    <span className="text-gate">
                      {target.candidateName}’s {ACTION_NOUN[target.action]}
                    </span>
                    {focused && (
                      <button type="button" onClick={() => setFocus(null)} className="hover:text-ink" aria-label="Clear selection">
                        ✕
                      </button>
                    )}
                  </span>
                )}
              </div>
            )}
            <div className="min-h-0 flex-1">
              <Thread components={{ Welcome, composerPlaceholder: placeholder }} autoFocus={false} />
            </div>
          </main>
        </div>
      </div>
      <CandidateDrawer />
    </AssistantRuntimeProvider>
  );
};

const JobStateControl: FC = () => {
  const job = useJobStore((s) => s.job)!;
  const refreshJob = useJobStore((s) => s.refreshJob);
  const [busy, setBusy] = useState(false);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await refreshJob();
    } finally {
      setBusy(false);
    }
  };
  const label = { DRAFT: "Not started", ACTIVE: "Running", PAUSED: "Paused", COMPLETE: "Complete" }[job.state];
  return (
    <div className="flex items-center gap-2 text-sm">
      <span className={cn("size-2 rounded-full", job.state === "ACTIVE" ? "bg-ok" : job.state === "PAUSED" ? "bg-gate" : "bg-line")} />
      <span className="text-ink">{label}</span>
      {job.state === "ACTIVE" && (
        <button type="button" disabled={busy} onClick={() => act(() => api.pauseJob(job.id))} className="text-quiet underline underline-offset-2 hover:text-ink">
          Pause
        </button>
      )}
      {job.state === "PAUSED" && (
        <button type="button" disabled={busy} onClick={() => act(() => api.resumeJob(job.id))} className="text-quiet underline underline-offset-2 hover:text-ink">
          Resume
        </button>
      )}
      {job.state === "DRAFT" && (
        <button type="button" disabled={busy} onClick={() => act(() => api.startJob(job.id))} className="rounded-md bg-ink px-2.5 py-1 text-white">
          Start recruiter
        </button>
      )}
    </div>
  );
};
