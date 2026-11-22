"use client";

import Link from "next/link";
import { useEffect, useState, type FC } from "react";
import { api } from "@/lib/api";
import type { JobDetail, ShortlistEntry } from "@/lib/types";
import { cn } from "@/lib/utils";
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

  useEffect(() => {
    Promise.all([api.getJob(jobId), api.shortlist(jobId)])
      .then(([j, r]) => {
        setJob(j);
        setRows(r);
      })
      .catch((e) => setErr(String(e)));
  }, [jobId]);

  return (
    <main className="mx-auto w-full max-w-3xl px-4 py-10">
      <Link href={`/jobs/${jobId}`} className="text-sm text-quiet hover:text-ink">
        Back to the recruiter
      </Link>
      <div className="mt-2 flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-3xl font-semibold tracking-tight text-ink">Shortlist{job ? `: ${job.title}` : ""}</h1>
        {job && rows && rows.length > 0 && (
          <button
            type="button"
            onClick={async () => {
              await navigator.clipboard.writeText(toMarkdown(job, rows));
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            }}
            className="rounded-md border border-line bg-white px-3 py-1.5 text-sm text-ink hover:bg-canvas"
          >
            {copied ? "Copied" : "Copy as Markdown"}
          </button>
        )}
      </div>
      {err && <p className="mt-6 text-danger">{err}</p>}
      {rows && rows.length === 0 && <p className="mt-6 text-quiet">Nobody has been evaluated yet. Candidates appear here after their interview is scored.</p>}
      <ol className="mt-6 grid gap-4">
        {rows?.map(({ candidate: c, evaluation: e }, i) => (
          <li key={c.id} className={cn("rounded-lg border bg-white p-5", c.state === "SHORTLISTED" ? "border-ok/40" : "border-line")}>
            <div className="flex items-baseline justify-between gap-3">
              <h2 className="text-lg font-semibold text-ink">
                <span className="mr-2 tabular-nums text-quiet">{i + 1}.</span>
                {c.name}
              </h2>
              <span className={cn("text-sm", c.state === "SHORTLISTED" ? "text-ok" : "text-quiet")}>
                {c.state === "SHORTLISTED" ? "Shortlisted" : "Evaluated, below the bar"}
              </span>
            </div>
            {c.headline && <p className="text-sm text-quiet">{c.headline}</p>}
            {e && (
              <div className="mt-4">
                <EvaluationBody evaluation={e} threshold={job?.scoreThreshold} />
              </div>
            )}
          </li>
        ))}
      </ol>
    </main>
  );
};
