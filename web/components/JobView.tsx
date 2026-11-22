"use client";

import Link from "next/link";
import { useEffect, useState, type FC } from "react";
import { api } from "@/lib/api";
import { useJobStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import { ActivityFeed } from "./ActivityFeed";
import { CandidateDrawer } from "./CandidateDrawer";
import { CandidateList } from "./CandidateList";
import { PolicyToggle } from "./PolicyToggle";
import { Btn } from "./tool-uis";
import { Wordmark } from "./TopBar";

export const JobView: FC<{ jobId: string }> = ({ jobId }) => {
  const connect = useJobStore((s) => s.connect);
  useEffect(() => connect(jobId), [jobId, connect]);

  const job = useJobStore((s) => s.job);
  const error = useJobStore((s) => s.error);
  const streamStatus = useJobStore((s) => s.streamStatus);
  const pendingCount = useJobStore((s) => Object.values(s.approvals).filter((a) => a.status === "pending").length);

  if (error && !job)
    return (
      <main className="mx-auto max-w-lg p-8 pt-20">
        <p className="text-ink">Couldn’t load this role: {error}</p>
        <p className="mt-2 text-sm text-quiet">Check that the API is running and NEXT_PUBLIC_API_URL points at it.</p>
        <Link href="/" className="mt-4 inline-block text-agent underline">
          Create a new role
        </Link>
      </main>
    );

  return (
    <>
      <div className="flex h-dvh flex-col">
        <header className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-line bg-canvas px-4 py-3 sm:px-6">
          <div className="min-w-0">
            <Wordmark className="text-xs text-quiet [&>span]:size-1.5" />
            <h1 className="truncate text-xl leading-tight font-semibold text-ink">{job?.title ?? "Loading…"}</h1>
          </div>
          {job && <JobStateControl />}
          <div className="ml-auto flex flex-wrap items-center gap-x-5 gap-y-2">
            {pendingCount > 0 && (
              <span className="rounded-full bg-gate/10 px-2.5 py-0.5 text-sm font-medium text-gate">
                {pendingCount} waiting for you
              </span>
            )}
            <PolicyToggle />
            <Link href={`/jobs/${jobId}/shortlist`} className="rounded-full border border-line bg-surface px-4 py-1.5 text-sm text-ink transition hover:border-ink/40">
              Shortlist
            </Link>
            <span
              className={cn("size-2 rounded-full", streamStatus === "live" ? "bg-ok" : streamStatus === "polling" ? "bg-gate" : "bg-line")}
              title={streamStatus === "live" ? "Live" : streamStatus === "polling" ? "Live updates via polling" : "Connecting"}
            />
          </div>
        </header>

        <div className="flex min-h-0 flex-1 flex-col md:flex-row">
          <main className="min-h-0 min-w-0 flex-1 overflow-y-auto bg-surface" aria-label="Candidates">
            <CandidateList />
          </main>
          <aside className="h-72 shrink-0 border-t border-line bg-canvas md:h-auto md:w-80 md:border-t-0 md:border-l" aria-label="Activity">
            <ActivityFeed />
          </aside>
        </div>
      </div>
      <CandidateDrawer />
    </>
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
    <div className="flex items-center gap-3 text-sm">
      <span className="flex items-center gap-2">
        <span className={cn("size-2 rounded-full", job.state === "ACTIVE" ? "bg-ok" : job.state === "PAUSED" ? "bg-gate" : "bg-line")} />
        <span className="text-ink">{label}</span>
      </span>
      {job.state === "ACTIVE" && (
        <Btn disabled={busy} onClick={() => act(() => api.pauseJob(job.id))}>
          Pause
        </Btn>
      )}
      {job.state === "PAUSED" && (
        <Btn tone="primary" disabled={busy} onClick={() => act(() => api.resumeJob(job.id))}>
          Resume
        </Btn>
      )}
      {job.state === "DRAFT" && (
        <button type="button" disabled={busy} onClick={() => act(() => api.startJob(job.id))} className="rounded-full bg-gate px-3.5 py-1 text-surface transition hover:bg-gate-deep">
          Start recruiter
        </button>
      )}
    </div>
  );
};
