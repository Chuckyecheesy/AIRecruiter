"use client";

import Link from "next/link";
import { useEffect, useState, type FC } from "react";
import { api } from "@/lib/api";
import { useJobStore } from "@/lib/store";
import type { JobDetail, ShortlistEntry } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CandidateAvatar } from "./CandidateAvatar";
import { CandidateDrawer } from "./CandidateDrawer";
import { TopBar } from "./TopBar";
import { EvaluationBody } from "./tool-uis";

function toMarkdown(job: JobDetail, rows: ShortlistEntry[]) {
  const lines = [`# Shortlist: ${job.title}`, ""];
  rows.forEach(({ candidate: c, evaluation: e }, i) => {
    lines.push(`## ${i + 1}. ${c.name}${c.score != null ? ` (${c.score})` : ""}`);
    if (c.headline) lines.push(`_${c.headline}_`);
    if (e) {
      lines.push("", e.summary, "");
      for (const k of e.criteria) lines.push(`- **${k.name}** (${Math.round(k.weight * 100)}%): ${Math.round(k.score)}. ${k.evidence}`);
    }
    lines.push("");
  });
  return lines.join("\n");
}

export const Shortlist: FC<{ jobId: string }> = ({ jobId }) => {
  const [job, setJob] = useState<JobDetail | null>(null);
  const [rows, setRows] = useState<ShortlistEntry[] | null>(null);
  const [copied, setCopied] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const openDrawer = useJobStore((s) => s.openDrawer);

  useEffect(() => {
    Promise.all([api.getJob(jobId), api.shortlist(jobId)])
      .then(([j, r]) => {
        setJob(j);
        setRows(r);
      })
      .catch((e) => setErr(String(e)));
  }, [jobId]);

  return (
    <>
    <TopBar>
      <Link href={`/jobs/${jobId}`} className="ml-auto text-sm text-quiet hover:text-ink">
        ← Back to the recruiter
      </Link>
    </TopBar>
    <main className="mx-auto w-full max-w-3xl px-4 pt-8 pb-16">
      <p className="label-mono text-gate">Shortlist</p>
      <div className="mt-2 flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-4xl leading-tight font-semibold text-ink sm:text-5xl">{job?.title ?? "Loading…"}</h1>
        {job && rows && rows.length > 0 && (
          <button
            type="button"
            onClick={async () => {
              await navigator.clipboard.writeText(toMarkdown(job, rows));
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            }}
            className="rounded-full border border-line bg-surface px-4 py-2 text-sm text-ink transition hover:border-ink/40"
          >
            {copied ? "Copied" : "Copy as Markdown"}
          </button>
        )}
      </div>
      <p className="mt-3 max-w-xl text-quiet">
        Everyone the recruiter has interviewed and scored, best first.{" "}
        {job ? `Shortlisted means they cleared your bar of ${job.scoreThreshold}.` : ""} Click a name for their full profile.
      </p>
      {err && <p className="mt-6 text-danger">{err}</p>}
      {rows && rows.length === 0 && <p className="mt-6 text-quiet">Nobody has been evaluated yet. Candidates appear here after their interview is scored.</p>}
      <ol className="mt-6 grid gap-4">
        {rows?.map(({ candidate: c, evaluation: e }, i) => (
          <li key={c.id} className={cn("rounded-2xl border bg-surface p-6", c.state === "SHORTLISTED" ? "border-ok/40" : "border-line")}>
            <div className="flex items-center justify-between gap-3">
              <button
                type="button"
                onClick={() => openDrawer(c.id)}
                className="group flex min-w-0 items-center gap-3.5 text-left"
                aria-label={`Open ${c.name}'s profile`}
              >
                <span className="font-mono text-sm tabular-nums text-quiet">{String(i + 1).padStart(2, "0")}</span>
                <CandidateAvatar name={c.name} size={48} className="transition group-hover:ring-ink/30" />
                <span className="min-w-0">
                  <span className="font-display block truncate text-xl font-semibold text-ink underline-offset-4 group-hover:underline">{c.name}</span>
                  {c.headline && <span className="block truncate text-sm text-quiet">{c.headline}</span>}
                </span>
              </button>
              <span className="flex shrink-0 items-center gap-3">
                {c.score != null && !e && <span className="font-display text-2xl font-semibold tabular-nums text-ink">{c.score}</span>}
                <span className={cn("rounded-full px-2.5 py-0.5 text-xs font-medium", c.state === "SHORTLISTED" ? "bg-ok/10 text-ok" : "bg-sunk text-quiet")}>
                  {c.state === "SHORTLISTED" ? "Shortlisted" : "Below the bar"}
                </span>
              </span>
            </div>
            {e && (
              <div className="mt-4">
                <EvaluationBody evaluation={e} threshold={job?.scoreThreshold} />
              </div>
            )}
          </li>
        ))}
      </ol>
    </main>
    <CandidateDrawer />
    </>
  );
};
