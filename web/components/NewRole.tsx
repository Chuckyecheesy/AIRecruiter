"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FC } from "react";
import { api } from "@/lib/api";
import type { JobDetail, JobSummary, Rubric } from "@/lib/types";
import { cn } from "@/lib/utils";
import type { RoleDraft } from "@/lib/voice/types";
import { TypeIntake, fieldClass } from "./intake/TypeIntake";
import { VoiceIntake } from "./intake/VoiceIntake";
import { TopBar } from "./TopBar";

const RESTRICTED_COPY: Record<string, string> = {
  send_outreach: "Email a candidate for the first time",
  send_followup: "Send one follow-up nudge",
  schedule_interview: "Create a voice interview link",
  reject_candidate_proposal: "Reject a candidate (always asks unless you list it)",
};

/** Typed-form defaults, so the demo can be run without typing. */
const DEMO_DRAFT: RoleDraft = {
  title: "AI Engineer (RAG + TypeScript)",
  mustHaves: ["RAG", "TypeScript", "LLM evaluation"],
  niceToHaves: ["React", "Python"],
  location: "Toronto / remote",
  seniority: "mid",
  notes: "",
};

type Mode = "voice" | "type";

export const NewRole: FC = () => {
  const router = useRouter();
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [mode, setMode] = useState<Mode>("voice");
  const [typedInitial, setTypedInitial] = useState<RoleDraft>(DEMO_DRAFT);
  const [formKey, setFormKey] = useState(0);
  const [step, setStep] = useState<"intake" | "review">("intake");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const [job, setJob] = useState<JobDetail | null>(null);
  const [rubric, setRubric] = useState<Rubric | null>(null);
  const [autoApprove, setAutoApprove] = useState(false);
  const [target, setTarget] = useState(3);
  const [threshold, setThreshold] = useState(75);

  useEffect(() => {
    api.listJobs().then(setJobs).catch(() => {});
  }, []);

  const createJob = async (draft: RoleDraft, freeText: string | null) => {
    setBusy(true);
    setErr(null);
    try {
      const j = await api.createJob({
        title: draft.title,
        mustHaves: draft.mustHaves,
        niceToHaves: draft.niceToHaves,
        location: draft.location || null,
        seniority: draft.seniority || null,
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

  const editInForm = (draft: RoleDraft) => {
    setTypedInitial(draft);
    setFormKey((k) => k + 1);
    setErr(null);
    setMode("type");
  };

  const totalWeight = rubric?.criteria.reduce((s, c) => s + (Number(c.weight) || 0), 0) ?? 0;

  return (
    <div className="flex min-h-dvh flex-col">
      <TopBar />
      <main className="mx-auto w-full max-w-6xl flex-1 px-4 pt-10 pb-16 sm:px-8 sm:pt-14">
        {step === "intake" && (
          <>
            <div className="flex flex-col gap-6 sm:flex-row sm:items-end sm:justify-between">
              <div>
                <p className="label-mono text-gate">New role</p>
                <h1 className="mt-2 text-5xl leading-[1.02] font-semibold text-ink sm:text-6xl">Who are we hiring?</h1>
                <p className="mt-3 max-w-xl text-[17px] text-quiet">
                  {mode === "voice"
                    ? "Just talk it through. The recruiter asks a few questions and writes the brief while you speak."
                    : "Write the brief yourself. The recruiter turns it into a scoring rubric."}
                </p>
              </div>
              <ModeSwitch mode={mode} onChange={(m) => {
                  setErr(null);
                  setMode(m);
                }} />
            </div>
            <div className="mt-12">
              {mode === "voice" ? (
                <VoiceIntake busy={busy} err={err} onSubmit={(d, md) => createJob(d, md)} onEdit={editInForm} />
              ) : (
                <TypeIntake key={formKey} initial={typedInitial} busy={busy} err={err} onSubmit={(d) => createJob(d, d.notes)} />
              )}
            </div>
            <YourRoles jobs={jobs} />
          </>
        )}

        {step === "review" && rubric && job && (
          <div className="mx-auto max-w-3xl">
            <p className="label-mono text-gate">Step 2 of 2 · Review</p>
            <h1 className="mt-2 text-4xl leading-tight font-semibold text-ink sm:text-5xl">{job.title}</h1>
            <p className="mt-2 text-quiet">Check how people will be scored, then let the recruiter loose.</p>

            <div className="mt-10 grid gap-6">
              <Card title="How candidates will be scored" hint="Edit names and weights. Weights should add up to 100%.">
                <ul className="grid gap-2">
                  {rubric.criteria.map((c, i) => (
                    <li key={i} className="flex items-center gap-2">
                      <input
                        aria-label="Criterion"
                        className={cn(fieldClass, "flex-1")}
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
                        className={cn(fieldClass, "w-20 tabular-nums")}
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
                        className="grid size-8 place-items-center rounded-full text-quiet hover:bg-sunk hover:text-danger"
                      >
                        ✕
                      </button>
                    </li>
                  ))}
                </ul>
                <div className="mt-3 flex items-center justify-between text-sm">
                  <button
                    type="button"
                    onClick={() => setRubric({ ...rubric, criteria: [...rubric.criteria, { name: "", weight: 0 }] })}
                    className="text-agent underline-offset-4 hover:underline"
                  >
                    + Add criterion
                  </button>
                  <span className={cn("label-mono tabular-nums", Math.abs(totalWeight - 1) > 0.011 ? "text-gate" : "text-quiet")}>
                    Total {Math.round(totalWeight * 100)}%
                  </span>
                </div>
                {rubric.interviewThemes.length > 0 && (
                  <div className="mt-6">
                    <h3 className="label-mono text-quiet">Interview topics</h3>
                    <ul className="mt-2 flex flex-wrap gap-1.5">
                      {rubric.interviewThemes.map((t, i) => (
                        <li key={i} className="rounded-full border border-line px-3 py-1 text-sm text-ink">
                          {t}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </Card>

              <Card title="What the recruiter needs your approval for">
                <ul className="grid gap-1.5 text-[15px] text-ink">
                  {job.permissionPolicy.restrictedActions.map((a) => (
                    <li key={a} className="flex items-baseline gap-2.5">
                      <span aria-hidden className="size-1.5 shrink-0 translate-y-[-2px] rounded-full bg-gate" />
                      {RESTRICTED_COPY[a] ?? a}
                    </li>
                  ))}
                </ul>
                <label className="mt-4 flex items-center gap-2.5 text-[15px]">
                  <input type="checkbox" checked={autoApprove} onChange={(e) => setAutoApprove(e.target.checked)} className="size-4 accent-gate" />
                  Auto-approve emails and interviews (you can switch this any time)
                </label>
              </Card>

              <Card title="When to stop">
                <div className="grid gap-5 sm:grid-cols-2">
                  <label className="grid gap-1.5">
                    <span className="label-mono text-quiet">Stop after shortlisting</span>
                    <input type="number" min={1} className={cn(fieldClass, "tabular-nums")} value={target} onChange={(e) => setTarget(Number(e.target.value))} />
                  </label>
                  <label className="grid gap-1.5">
                    <span className="label-mono text-quiet">Shortlist at score</span>
                    <input
                      type="number"
                      min={0}
                      max={100}
                      className={cn(fieldClass, "tabular-nums")}
                      value={threshold}
                      onChange={(e) => setThreshold(Number(e.target.value))}
                    />
                  </label>
                </div>
              </Card>
            </div>

            {err && <p className="mt-4 text-sm text-danger">{err}</p>}
            <div className="mt-8 flex items-center gap-4">
              <button type="button" onClick={start} disabled={busy} className="rounded-full bg-gate px-6 py-3 font-medium text-surface transition hover:bg-gate-deep disabled:opacity-60">
                {busy ? "Starting…" : "Start recruiter →"}
              </button>
              <button type="button" onClick={() => setStep("intake")} className="text-sm text-quiet underline-offset-4 hover:text-ink hover:underline">
                Back
              </button>
            </div>
          </div>
        )}
      </main>
    </div>
  );
};

const ModeSwitch: FC<{ mode: Mode; onChange: (m: Mode) => void }> = ({ mode, onChange }) => (
  <div role="tablist" aria-label="How to describe the role" className="inline-flex shrink-0 self-start rounded-full bg-sunk p-1 sm:self-auto">
    {(
      [
        ["voice", "Speak"],
        ["type", "Type"],
      ] as const
    ).map(([m, label]) => (
      <button
        key={m}
        role="tab"
        type="button"
        aria-selected={mode === m}
        onClick={() => onChange(m)}
        className={cn(
          "rounded-full px-5 py-2 text-sm font-medium transition",
          mode === m ? "bg-surface text-ink shadow-[0_1px_2px_rgba(43,36,32,0.12)]" : "text-quiet hover:text-ink",
        )}
      >
        {label}
      </button>
    ))}
  </div>
);

const Card: FC<{ title: string; hint?: string; children: React.ReactNode }> = ({ title, hint, children }) => (
  <section className="rounded-2xl border border-line bg-surface p-6">
    <h2 className="text-xl font-semibold text-ink">{title}</h2>
    {hint && <p className="mt-0.5 text-sm text-quiet">{hint}</p>}
    <div className="mt-4">{children}</div>
  </section>
);

const JOB_STATE_COPY: Record<string, string> = { ACTIVE: "Running", PAUSED: "Paused", DRAFT: "Not started", COMPLETE: "Complete" };

const YourRoles: FC<{ jobs: JobSummary[] }> = ({ jobs }) => {
  if (!jobs.length) return null;
  return (
    <section className="mt-20 border-t border-line pt-8">
      <h2 className="label-mono text-quiet">Your roles</h2>
      <ul className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {jobs.map((j) => (
          <li key={j.id}>
            <Link href={`/jobs/${j.id}`} className="group block rounded-2xl border border-line bg-surface/70 px-4 py-3.5 transition hover:border-ink/25 hover:bg-surface">
              <span className="font-display block truncate text-[17px] font-medium text-ink">{j.title}</span>
              <span className="mt-1 flex items-center gap-2 text-sm text-quiet">
                <span className={cn("size-1.5 rounded-full", j.state === "ACTIVE" ? "bg-ok" : j.state === "PAUSED" ? "bg-gate" : "bg-line")} />
                {JOB_STATE_COPY[j.state] ?? j.state}
                {j.pendingApprovals > 0 && <span className="text-gate">· {j.pendingApprovals} waiting for you</span>}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
};
