"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FC, type FormEvent } from "react";
import { api } from "@/lib/api";
import type { JobDetail, JobSummary, Rubric } from "@/lib/types";
import { cn } from "@/lib/utils";

const splitList = (s: string) =>
  s
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean);

const input =
  "w-full rounded-md border border-line bg-white px-3 py-2 text-[15px] text-ink outline-none focus:border-ink focus-visible:outline-none";

const RESTRICTED_COPY: Record<string, string> = {
  send_outreach: "Email a candidate for the first time",
  send_followup: "Send one follow-up nudge",
  schedule_interview: "Create a voice interview link",
  reject_candidate_proposal: "Reject a candidate (always asks unless you list it)",
};

export const NewRole: FC = () => {
  const router = useRouter();
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [step, setStep] = useState<"form" | "review">("form");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const [title, setTitle] = useState("AI Engineer (RAG + TypeScript)");
  const [must, setMust] = useState("RAG, TypeScript, LLM evaluation");
  const [nice, setNice] = useState("React, Python");
  const [location, setLocation] = useState("Toronto / remote");
  const [seniority, setSeniority] = useState("mid");
  const [freeText, setFreeText] = useState("");

  const [job, setJob] = useState<JobDetail | null>(null);
  const [rubric, setRubric] = useState<Rubric | null>(null);
  const [autoApprove, setAutoApprove] = useState(false);
  const [target, setTarget] = useState(3);
  const [threshold, setThreshold] = useState(75);

  useEffect(() => {
    api.listJobs().then(setJobs).catch(() => {});
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      const j = await api.createJob({
        title,
        mustHaves: splitList(must),
        niceToHaves: splitList(nice),
        location: location || null,
        seniority: seniority || null,
        freeText: freeText || null,
      });
      setJob(j);
      setRubric(j.rubric ?? { criteria: [], interviewThemes: [] });
      setTarget(j.shortlistTarget);
      setThreshold(j.scoreThreshold);
      setAutoApprove(j.permissionPolicy.autoApprove);
      setStep("review");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const start = async () => {
    if (!job || !rubric) return;
    setBusy(true);
    setErr(null);
    try {
      await api.patchJob(job.id, {
        rubric,
        shortlistTarget: target,
        scoreThreshold: threshold,
        permissionPolicy: { ...job.permissionPolicy, autoApprove },
      });
      await api.startJob(job.id);
      router.push(`/jobs/${job.id}`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  const totalWeight = rubric?.criteria.reduce((s, c) => s + (Number(c.weight) || 0), 0) ?? 0;

  return (
    <main className="mx-auto grid w-full max-w-5xl gap-10 px-4 py-10 md:grid-cols-[1fr_16rem]">
      <div className="min-w-0">
        <h1 className="text-3xl font-semibold tracking-tight text-ink">Hire for a new role</h1>
        <p className="mt-2 max-w-prose text-quiet">
          Describe the role. The recruiter finds people, researches them and drafts outreach on its own, and asks you before it
          contacts, interviews or rejects anyone.
        </p>

        {step === "form" && (
          <form onSubmit={submit} className="mt-8 grid gap-5">
            <label className="grid gap-1.5">
              <span className="text-sm font-medium text-ink">Role title</span>
              <input className={input} value={title} onChange={(e) => setTitle(e.target.value)} required />
            </label>
            <div className="grid gap-5 sm:grid-cols-2">
              <label className="grid gap-1.5">
                <span className="text-sm font-medium text-ink">Must-have skills</span>
                <input className={input} value={must} onChange={(e) => setMust(e.target.value)} placeholder="Comma separated" />
              </label>
              <label className="grid gap-1.5">
                <span className="text-sm font-medium text-ink">Nice to have</span>
                <input className={input} value={nice} onChange={(e) => setNice(e.target.value)} placeholder="Comma separated" />
              </label>
              <label className="grid gap-1.5">
                <span className="text-sm font-medium text-ink">Location</span>
                <input className={input} value={location} onChange={(e) => setLocation(e.target.value)} />
              </label>
              <label className="grid gap-1.5">
                <span className="text-sm font-medium text-ink">Seniority</span>
                <select className={input} value={seniority} onChange={(e) => setSeniority(e.target.value)}>
                  <option value="">Any</option>
                  <option value="junior">Junior</option>
                  <option value="mid">Mid</option>
                  <option value="senior">Senior</option>
                  <option value="staff">Staff</option>
                </select>
              </label>
            </div>
            <label className="grid gap-1.5">
              <span className="text-sm font-medium text-ink">Anything else the recruiter should know</span>
              <textarea className={cn(input, "min-h-24")} value={freeText} onChange={(e) => setFreeText(e.target.value)} />
            </label>
            {err && <p className="text-sm text-danger">{err}</p>}
            <div>
              <button type="submit" disabled={busy} className="rounded-md bg-ink px-4 py-2 font-medium text-white disabled:opacity-60">
                {busy ? "Writing the rubric…" : "Generate rubric"}
              </button>
            </div>
          </form>
        )}

        {step === "review" && rubric && (
          <div className="mt-8 grid gap-8">
            <section>
              <h2 className="text-lg font-semibold text-ink">How candidates will be scored</h2>
              <p className="text-sm text-quiet">Edit names and weights. Weights should add up to 100%.</p>
              <ul className="mt-3 grid gap-2">
                {rubric.criteria.map((c, i) => (
                  <li key={i} className="flex items-center gap-2">
                    <input
                      aria-label="Criterion"
                      className={cn(input, "flex-1")}
                      value={c.name}
                      onChange={(e) => {
                        const criteria = [...rubric.criteria];
                        criteria[i] = { ...c, name: e.target.value };
                        setRubric({ ...rubric, criteria });
                      }}
                    />
                    <input
                      aria-label={`Weight for ${c.name}`}
                      type="number"
                      min={0}
                      max={100}
                      className={cn(input, "w-20 tabular-nums")}
                      value={Math.round(c.weight * 100)}
                      onChange={(e) => {
                        const criteria = [...rubric.criteria];
                        criteria[i] = { ...c, weight: Number(e.target.value) / 100 };
                        setRubric({ ...rubric, criteria });
                      }}
                    />
                    <span className="text-sm text-quiet">%</span>
                    <button
                      type="button"
                      aria-label={`Remove ${c.name}`}
                      onClick={() => setRubric({ ...rubric, criteria: rubric.criteria.filter((_, j) => j !== i) })}
                      className="px-2 text-quiet hover:text-danger"
                    >
                      ✕
                    </button>
                  </li>
                ))}
              </ul>
              <div className="mt-2 flex items-center justify-between text-sm">
                <button
                  type="button"
                  onClick={() => setRubric({ ...rubric, criteria: [...rubric.criteria, { name: "", weight: 0 }] })}
                  className="text-agent underline underline-offset-2"
                >
                  Add criterion
                </button>
                <span className={cn("tabular-nums", Math.abs(totalWeight - 1) > 0.011 ? "text-gate" : "text-quiet")}>
                  Total {Math.round(totalWeight * 100)}%
                </span>
              </div>
              {rubric.interviewThemes.length > 0 && (
                <>
                  <h3 className="mt-5 text-sm font-medium text-ink">Interview topics</h3>
                  <ul className="mt-1 list-disc pl-5 text-[15px] text-ink">
                    {rubric.interviewThemes.map((t, i) => (
                      <li key={i}>{t}</li>
                    ))}
                  </ul>
                </>
              )}
            </section>

            <section>
              <h2 className="text-lg font-semibold text-ink">What the recruiter needs your approval for</h2>
              <ul className="mt-2 grid gap-1 text-[15px] text-ink">
                {job!.permissionPolicy.restrictedActions.map((a) => (
                  <li key={a} className="flex items-baseline gap-2">
                    <span aria-hidden className="text-gate">●</span>
                    {RESTRICTED_COPY[a] ?? a}
                  </li>
                ))}
              </ul>
              <label className="mt-3 flex items-center gap-2 text-[15px]">
                <input type="checkbox" checked={autoApprove} onChange={(e) => setAutoApprove(e.target.checked)} className="size-4 accent-ink" />
                Auto-approve emails and interviews (you can switch this any time)
              </label>
            </section>

            <section className="grid gap-5 sm:grid-cols-2">
              <label className="grid gap-1.5">
                <span className="text-sm font-medium text-ink">Stop after shortlisting</span>
                <input type="number" min={1} className={cn(input, "tabular-nums")} value={target} onChange={(e) => setTarget(Number(e.target.value))} />
              </label>
              <label className="grid gap-1.5">
                <span className="text-sm font-medium text-ink">Shortlist at score</span>
                <input
                  type="number"
                  min={0}
                  max={100}
                  className={cn(input, "tabular-nums")}
                  value={threshold}
                  onChange={(e) => setThreshold(Number(e.target.value))}
                />
              </label>
            </section>

            {err && <p className="text-sm text-danger">{err}</p>}
            <div className="flex gap-3">
              <button type="button" onClick={start} disabled={busy} className="rounded-md bg-ink px-4 py-2 font-medium text-white disabled:opacity-60">
                {busy ? "Starting…" : "Start recruiter"}
              </button>
              <button type="button" onClick={() => setStep("form")} className="rounded-md border border-line px-4 py-2 text-ink">
                Back
              </button>
            </div>
          </div>
        )}
      </div>

      <aside>
        <h2 className="text-sm font-semibold text-ink">Your roles</h2>
        {jobs.length ? (
          <ul className="mt-2 grid gap-1.5">
            {jobs.map((j) => (
              <li key={j.id}>
                <Link href={`/jobs/${j.id}`} className="block rounded-md border border-line bg-white px-3 py-2 hover:border-quiet/50">
                  <span className="block truncate text-sm font-medium text-ink">{j.title}</span>
                  <span className="text-xs text-quiet">
                    {j.state === "ACTIVE" ? "Running" : j.state === "PAUSED" ? "Paused" : j.state === "DRAFT" ? "Not started" : "Complete"}
                    {j.pendingApprovals > 0 && <span className="text-gate">, {j.pendingApprovals} waiting for you</span>}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-2 text-sm text-quiet">None yet.</p>
        )}
      </aside>
    </main>
  );
};
