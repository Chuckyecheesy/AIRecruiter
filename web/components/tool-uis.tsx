"use client";

import { useState, type FC, type ReactNode } from "react";
import { api } from "@/lib/api";
import { fmtDuration } from "@/lib/activity";
import { useCandidateDetail, useJobStore } from "@/lib/store";
import type { Approval, Evaluation, Interview } from "@/lib/types";
import { cn } from "@/lib/utils";

/* ───────────────────────── shared bits ───────────────────────── */

export const CandidateLink: FC<{ id: string | null | undefined; name: string | null | undefined }> = ({ id, name }) => {
  const open = useJobStore((s) => s.openDrawer);
  if (!id) return <span>{name ?? "candidate"}</span>;
  return (
    <button type="button" onClick={() => open(id)} className="font-medium text-ink underline decoration-line underline-offset-2 hover:decoration-ink">
      {name ?? "candidate"}
    </button>
  );
};

function useApproval(id: string | undefined): Approval | null {
  return useJobStore((s) => (id ? (s.approvals[id] ?? null) : null));
}

function useApprovalActions(a: Approval | null) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const refresh = useJobStore((s) => s.refreshApprovals);
  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return {
    busy,
    err,
    approve: (payload?: Record<string, unknown>) => a && run(() => api.approve(a.id, payload)),
    reject: (feedback: string) => a && run(() => api.reject(a.id, feedback)),
  };
}

export const Btn: FC<{
  onClick?: () => void;
  disabled?: boolean;
  tone?: "primary" | "plain" | "danger";
  children: ReactNode;
  type?: "button" | "submit";
}> = ({ onClick, disabled, tone = "plain", children, type = "button" }) => (
  <button
    type={type}
    onClick={onClick}
    disabled={disabled}
    className={cn(
      "rounded-full px-4 py-1.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gate disabled:opacity-50",
      tone === "primary" && "bg-ink text-surface hover:bg-ink/85",
      tone === "plain" && "border border-line bg-surface text-ink hover:border-ink/40",
      tone === "danger" && "border border-danger/30 bg-surface text-danger hover:bg-danger/5",
    )}
  >
    {children}
  </button>
);

const ACTION_TITLE: Record<string, string> = {
  send_outreach: "Send outreach email",
  send_followup: "Send follow-up email",
  schedule_interview: "Schedule voice interview",
  reject_candidate_proposal: "Reject candidate",
};

/** The gate: every restricted action renders inside this frame. */
const Gate: FC<{ approval: Approval | null; children: ReactNode; footer?: ReactNode }> = ({ approval, children, footer }) => {
  if (!approval)
    return <div className="h-28 animate-pulse rounded-2xl border border-line bg-surface motion-reduce:animate-none" />;
  const pending = approval.status === "pending";
  return (
    <section
      aria-label={`${ACTION_TITLE[approval.action]} for ${approval.candidateName}`}
      className={cn(
        "overflow-hidden rounded-2xl border bg-surface",
        pending ? "border-gate/50 shadow-[0_0_0_4px_rgba(194,86,46,0.08)]" : "border-line",
      )}
    >
      <header className="flex flex-wrap items-baseline justify-between gap-2 px-5 pt-4">
        <h3 className="text-[17px] font-semibold text-ink">
          {ACTION_TITLE[approval.action]} <span className="font-normal text-quiet">for</span>{" "}
          <CandidateLink id={approval.candidateId} name={approval.candidateName} />
        </h3>
        <StatusBadge approval={approval} />
      </header>
      {approval.reasoning && <p className="px-5 pt-1 text-sm text-quiet">Agent’s reasoning: {approval.reasoning}</p>}
      <div className="px-5 py-3.5">{children}</div>
      {footer && <footer className="border-t border-line bg-sunk/50 px-5 py-3">{footer}</footer>}
    </section>
  );
};

const StatusBadge: FC<{ approval: Approval }> = ({ approval }) => {
  const by = approval.decidedBy === "auto" ? "auto-approved" : "approved by you";
  const map: Record<string, [string, string]> = {
    pending: ["Waiting for your approval", "bg-gate/10 text-gate"],
    approved: [by, "bg-ok/10 text-ok"],
    executed: [approval.decidedBy === "auto" ? "Auto-approved, done" : "Approved, done", "bg-ok/10 text-ok"],
    rejected: ["Sent back", "bg-sunk text-quiet"],
    expired: ["Expired", "bg-sunk text-quiet"],
  };
  const [label, cls] = map[approval.status] ?? [approval.status, "bg-sunk text-quiet"];
  return <span className={cn("rounded-full px-2.5 py-0.5 text-xs font-medium", cls)}>{label}</span>;
};

/** Approve / reject-with-feedback row, shared by every gate. */
const Decide: FC<{
  approval: Approval;
  approveLabel?: string;
  rejectLabel?: string;
  feedbackPlaceholder?: string;
  editedPayload?: () => Record<string, unknown> | undefined;
}> = ({ approval, approveLabel = "Approve", rejectLabel = "Reject with feedback", feedbackPlaceholder = "What should change?", editedPayload }) => {
  const { busy, err, approve, reject } = useApprovalActions(approval);
  const [rejecting, setRejecting] = useState(false);
  const [feedback, setFeedback] = useState("");

  if (approval.status === "rejected")
    return (
      <p className="text-sm text-quiet">
        You sent this back{approval.feedback ? <>: “<span className="text-ink">{approval.feedback}</span>”</> : "."}
      </p>
    );
  if (approval.status !== "pending")
    return <p className="text-sm text-quiet">{approval.decidedBy === "auto" ? "Approved automatically by policy." : "You approved this."}</p>;

  if (rejecting)
    return (
      <form
        className="flex flex-col gap-2 sm:flex-row"
        onSubmit={(e) => {
          e.preventDefault();
          if (feedback.trim()) reject(feedback.trim());
        }}
      >
        <input
          autoFocus
          value={feedback}
          onChange={(e) => setFeedback(e.target.value)}
          placeholder={feedbackPlaceholder}
          className="min-w-0 flex-1 rounded-full border border-line bg-surface px-4 py-1.5 text-sm outline-none focus:border-ink/60"
        />
        <div className="flex gap-2">
          <Btn type="submit" tone="primary" disabled={busy || !feedback.trim()}>
            Send back
          </Btn>
          <Btn onClick={() => setRejecting(false)}>Cancel</Btn>
        </div>
      </form>
    );

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Btn tone="primary" disabled={busy} onClick={() => approve(editedPayload?.())}>
        {approveLabel}
      </Btn>
      <Btn disabled={busy} onClick={() => setRejecting(true)}>
        {rejectLabel}
      </Btn>
      {err && <span className="text-sm text-danger">{err}</span>}
    </div>
  );
};

/* ───────────────────────── EmailDraftCard ───────────────────────── */

export const EmailDraftCard: FC<{ args: { approvalId: string } }> = ({ args }) => {
  const approval = useApproval(args.approvalId);
  const [subject, setSubject] = useState<string | null>(null);
  const [body, setBody] = useState<string | null>(null);
  if (!approval) return <Gate approval={null}>{null}</Gate>;
  const p = approval.payload;
  const editable = approval.status === "pending";
  const s = subject ?? p.subject ?? "";
  const b = body ?? p.body ?? "";
  const edited = (subject !== null && subject !== p.subject) || (body !== null && body !== p.body);

  return (
    <Gate
      approval={approval}
      footer={
        <Decide
          approval={approval}
          approveLabel={edited ? "Approve with my edits" : "Approve and send"}
          feedbackPlaceholder="e.g. shorter, mention the AI study assistant"
          editedPayload={() => (edited ? { ...p, subject: s, body: b } : undefined)}
        />
      }
    >
      <div className="rounded-xl border border-line">
        <div className="flex gap-3 border-b border-line px-3 py-2 text-sm">
          <span className="w-14 shrink-0 text-quiet">To</span>
          <span className="truncate text-ink">{p.to ?? approval.candidateName}</span>
          <span className="ml-auto shrink-0 text-xs text-quiet">simulated, never really sent</span>
        </div>
        <label className="flex items-center gap-3 border-b border-line px-3 py-2 text-sm">
          <span className="w-14 shrink-0 text-quiet">Subject</span>
          <input
            value={s}
            readOnly={!editable}
            onChange={(e) => setSubject(e.target.value)}
            className="min-w-0 flex-1 bg-transparent font-medium text-ink outline-none"
          />
        </label>
        <textarea
          aria-label="Email body"
          value={b}
          readOnly={!editable}
          onChange={(e) => setBody(e.target.value)}
          rows={Math.min(12, Math.max(4, b.split("\n").length + 1))}
          className="block w-full resize-y bg-transparent px-3 py-2.5 text-[15px] leading-relaxed text-ink outline-none"
        />
      </div>
    </Gate>
  );
};

/* ───────────────────────── Interview ───────────────────────── */

const InterviewLive: FC<{ interview: Interview }> = ({ interview }) => {
  const [copied, setCopied] = useState(false);
  const [open, setOpen] = useState(false);
  const label = {
    scheduled: "Link is live, waiting for the candidate to join",
    in_progress: "Interview in progress",
    completed: `Completed${interview.durationS ? ` in ${fmtDuration(interview.durationS)}` : ""}`,
    expired: "Link expired",
  }[interview.status];
  return (
    <div className="mt-3 rounded-xl border border-line p-3">
      <p className="flex items-center gap-2 text-sm text-ink">
        <span
          className={cn(
            "inline-block size-2 rounded-full",
            interview.status === "completed" ? "bg-ok" : interview.status === "expired" ? "bg-quiet" : "animate-pulse bg-agent motion-reduce:animate-none",
          )}
        />
        {label}
      </p>
      {interview.joinUrl && interview.status !== "completed" && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <a href={interview.joinUrl} target="_blank" rel="noreferrer" className="truncate text-sm text-agent underline underline-offset-2">
            Open interview
          </a>
          <Btn
            onClick={async () => {
              await navigator.clipboard.writeText(interview.joinUrl!);
              setCopied(true);
              setTimeout(() => setCopied(false), 1500);
            }}
          >
            {copied ? "Copied" : "Copy join link"}
          </Btn>
        </div>
      )}
      {interview.transcript.length > 0 && (
        <div className="mt-2">
          <button type="button" onClick={() => setOpen(!open)} className="text-sm text-quiet underline underline-offset-2">
            {open ? "Hide transcript" : `Show transcript (${interview.transcript.length} turns)`}
          </button>
          {open && (
            <ol className="mt-2 space-y-1.5 text-sm">
              {interview.transcript.map((t, i) => (
                <li key={i} className={cn(t.role === "agent" ? "text-quiet" : "text-ink")}>
                  <span className="font-medium">{t.role === "agent" ? "Interviewer" : "Candidate"}:</span> {t.text ?? t.message}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </div>
  );
};

export const InterviewCard: FC<{ args: { approvalId: string; candidateId: string } }> = ({ args }) => {
  const approval = useApproval(args.approvalId);
  const detail = useCandidateDetail(approval?.status === "pending" ? null : args.candidateId);
  const questions: string[] = approval?.payload?.questions ?? [];
  const interview = detail?.interview && approval && detail.interview.createdAt >= approval.createdAt ? detail.interview : null;
  return (
    <Gate approval={approval} footer={approval && <Decide approval={approval} approveLabel="Approve and create link" feedbackPlaceholder="e.g. not yet, wait for their question" />}>
      {questions.length > 0 && (
        <>
          <p className="text-sm text-quiet">The voice agent will ask:</p>
          <ol className="mt-1 list-decimal space-y-1 pl-5 text-[15px] text-ink">
            {questions.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ol>
        </>
      )}
      {interview && <InterviewLive interview={interview} />}
    </Gate>
  );
};

/* ───────────────────────── RejectionCard ───────────────────────── */

export const RejectionCard: FC<{ args: { approvalId: string } }> = ({ args }) => {
  const approval = useApproval(args.approvalId);
  const p = approval?.payload ?? {};
  return (
    <Gate
      approval={approval}
      footer={approval && <Decide approval={approval} approveLabel="Reject candidate" rejectLabel="Keep candidate" feedbackPlaceholder="Why keep them?" />}
    >
      <div className="flex items-start gap-4">
        {p.score != null && <ScoreDial score={p.score} />}
        <div className="min-w-0">
          {p.reason && <p className="text-[15px] text-ink">{p.reason}</p>}
          {Array.isArray(p.evidence) && p.evidence.length > 0 && (
            <ul className="mt-1.5 list-disc space-y-0.5 pl-5 text-sm text-quiet">
              {p.evidence.map((e: string, i: number) => (
                <li key={i}>{e}</li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </Gate>
  );
};

/* ───────────────────────── Evaluation ───────────────────────── */

export const ScoreDial: FC<{ score: number; threshold?: number }> = ({ score, threshold }) => {
  const pass = threshold == null ? score >= 75 : score >= threshold;
  return (
    <div
      className={cn(
        "flex size-14 shrink-0 flex-col items-center justify-center rounded-full border-2 tabular-nums",
        pass ? "border-ok text-ok" : "border-danger text-danger",
      )}
      aria-label={`Score ${score} out of 100`}
    >
      <span className="text-lg leading-none font-semibold">{score}</span>
    </div>
  );
};

export const EvaluationBody: FC<{ evaluation: Evaluation; threshold?: number }> = ({ evaluation, threshold }) => (
  <div>
    <div className="flex items-start gap-4">
      <ScoreDial score={evaluation.score} threshold={threshold} />
      <div>
        <p className="text-sm font-medium text-ink">
          Recommendation: {{ shortlist: "shortlist", reject: "reject", borderline: "borderline, your call" }[evaluation.recommendation]}
        </p>
        <p className="mt-0.5 text-[15px] text-ink">{evaluation.summary}</p>
      </div>
    </div>
    <ul className="mt-3 space-y-2.5">
      {evaluation.criteria.map((c) => (
        <li key={c.name}>
          <div className="flex items-baseline justify-between gap-2 text-sm">
            <span className="text-ink">
              {c.name} <span className="text-quiet">({Math.round(c.weight * 100)}%)</span>
            </span>
            <span className="tabular-nums text-ink">{Math.round(c.score)}</span>
          </div>
          <div className="mt-1 h-1.5 rounded-full bg-canvas">
            <div className="h-full rounded-full bg-agent" style={{ width: `${Math.max(2, Math.min(100, c.score))}%` }} />
          </div>
          {c.evidence && <p className="mt-1 text-sm text-quiet">{c.evidence}</p>}
        </li>
      ))}
    </ul>
  </div>
);

/* ───────────────────────── Needs human ───────────────────────── */

export const NeedsHumanCard: FC<{ args: { candidateId: string; error: string; traceUrl: string | null } }> = ({ args }) => {
  const cand = useJobStore((s) => s.candidates.find((c) => c.id === args.candidateId));
  const refresh = useJobStore((s) => s.refreshCandidates);
  const [busy, setBusy] = useState(false);
  const stillStuck = cand?.state === "NEEDS_HUMAN";
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await refresh();
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className={cn("rounded-2xl border bg-surface px-4 py-3", stillStuck ? "border-danger/50 shadow-[0_0_0_4px_rgba(179,38,30,0.07)]" : "border-line")}>
      <h3 className="text-[15px] font-semibold text-ink">
        <CandidateLink id={args.candidateId} name={cand?.name} /> needs you
      </h3>
      <p className="mt-1 text-sm text-ink">{args.error || "The agent stopped after repeated failures."}</p>
      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        {args.traceUrl ? (
          <a href={args.traceUrl} target="_blank" rel="noreferrer" className="text-sm text-agent underline underline-offset-2">
            Open trace in Langfuse
          </a>
        ) : null}
        {stillStuck ? (
          <>
            <Btn tone="primary" disabled={busy} onClick={() => act(() => api.resumeCandidate(args.candidateId))}>
              Resume
            </Btn>
            <Btn tone="danger" disabled={busy} onClick={() => act(() => api.skipCandidate(args.candidateId))}>
              Skip candidate
            </Btn>
          </>
        ) : (
          <span className="text-sm text-quiet">Resolved.</span>
        )}
      </div>
    </section>
  );
};

/* ───────────────────────── quiet lines ───────────────────────── */

export const DecisionNote: FC<{ args: { candidateId: string | null; candidateName: string | null; label: string; reasoning: string } }> = ({ args }) => {
  const [open, setOpen] = useState(false);
  return (
    <div className="my-1 text-sm">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="group flex w-full items-baseline gap-2 text-left text-quiet hover:text-ink"
      >
        <span aria-hidden className="text-agent">◆</span>
        <span>
          {args.candidateName ? <span className="text-ink">{args.candidateName}</span> : "Agent"}: {args.label}
          {!open && args.reasoning && <span className="text-quiet/80"> because {lowerFirst(args.reasoning)}</span>}
        </span>
      </button>
      {open && <p className="ml-5 mt-0.5 text-ink">{args.reasoning}</p>}
    </div>
  );
};

export const StatusLine: FC<{ args: { icon: string; text: string; tone?: "warn" } }> = ({ args }) => (
  <p className={cn("my-1 flex items-baseline gap-2 text-sm", args.tone === "warn" ? "text-danger" : "text-ink")}>
    <span aria-hidden className="w-4 shrink-0 text-center">{args.icon}</span>
    <span>{args.text}</span>
  </p>
);

export const DuplicateNote: FC<{ args: { name: string; existingCandidateId: string; reason?: string | null } }> = ({ args }) => {
  const existing = useJobStore((s) => s.candidates.find((c) => c.id === args.existingCandidateId));
  return (
    <p className="my-1 flex items-baseline gap-2 text-sm text-quiet">
      <span aria-hidden className="w-4 shrink-0 text-center">⧉</span>
      <span>
        Skipped “{args.name}”: already in the pipeline as <CandidateLink id={args.existingCandidateId} name={existing?.name ?? "an existing candidate"} />
        {args.reason ? ` (${args.reason})` : ""}.
      </span>
    </p>
  );
};

function lowerFirst(s: string) {
  // Keep acronyms ("CEO asked…") intact.
  if (!s || (s.length > 1 && s[1] === s[1].toUpperCase() && /[A-Z]/.test(s[1]))) return s;
  return s[0].toLowerCase() + s.slice(1);
}

/* ───────────────────────── inline approval ───────────────────────── */

/** Picks the right gate card for a pending (or decided) approval, rendered inline under its candidate. */
export const ApprovalCard: FC<{ approvalId: string; candidateId: string }> = ({ approvalId, candidateId }) => {
  const approval = useApproval(approvalId);
  if (!approval) return <Gate approval={null}>{null}</Gate>;
  switch (approval.action) {
    case "schedule_interview":
      return <InterviewCard args={{ approvalId, candidateId }} />;
    case "reject_candidate_proposal":
      return <RejectionCard args={{ approvalId }} />;
    default:
      return <EmailDraftCard args={{ approvalId }} />;
  }
};
