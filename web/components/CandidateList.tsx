"use client";

import { useMemo, type FC } from "react";
import { ACTION_LABEL } from "@/lib/activity";
import { useJobStore } from "@/lib/store";
import { PIPELINE_COLUMNS, type Candidate, type EventRow } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CandidateAvatar } from "./CandidateAvatar";
import { ApprovalCard, NeedsHumanCard } from "./tool-uis";

const STATE_LABEL: Record<string, string> = {
  DISCOVERED: "Found",
  RESEARCHED: "Researched",
  CONTACT_FOUND: "Contact found",
  OUTREACH_DRAFTED: "Draft ready",
  APPROVED: "Approved to send",
  CONTACTED: "Emailed",
  INTERESTED: "Interested",
  INTERVIEWING: "Interviewing",
  INTERVIEWED: "Interviewed",
  EVALUATED: "Evaluated",
  SHORTLISTED: "Shortlisted",
  REJECTED: "Rejected",
  DECLINED: "Declined",
  STALE: "No reply",
  NEEDS_HUMAN: "Needs you",
};

const needsYou = (c: Candidate) => c.needsHuman || c.state === "NEEDS_HUMAN" || !!c.pendingApprovalId;

type Latest = { decision?: { action: string; reasoning: string }; escalation?: { error: string; traceUrl: string | null } };

/** One pass over the event log: each candidate's latest decision and latest escalation. */
function latestByCandidate(events: EventRow[]): Record<string, Latest> {
  const out: Record<string, Latest> = {};
  for (const ev of events) {
    if (!ev.candidateId) continue;
    const cur = (out[ev.candidateId] ??= {});
    const d = ev.payload?.decision;
    if (d?.action) cur.decision = { action: d.action, reasoning: d.reasoning ?? "" };
    if (ev.type === "Escalated") cur.escalation = { error: ev.payload?.error ?? "", traceUrl: ev.payload?.trace_url ?? null };
  }
  return out;
}

export const CandidateList: FC = () => {
  const candidates = useJobStore((s) => s.candidates);
  const events = useJobStore((s) => s.events);
  const latest = useMemo(() => latestByCandidate(events), [events]);

  if (!candidates.length)
    return (
      <div className="px-4 py-10 sm:px-6">
        <p className="font-display text-2xl font-medium text-ink">No candidates yet.</p>
        <p className="mt-1 text-sm text-quiet">They show up here as the agent finds them. Anything that needs your approval appears at the top.</p>
      </div>
    );

  const urgent = candidates.filter(needsYou);
  const groups = PIPELINE_COLUMNS.map((col) => ({
    title: col.title,
    items: candidates.filter((c) => !needsYou(c) && col.states.includes(c.state)),
  }));

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-7 px-4 py-5 sm:px-6">
      {urgent.length > 0 && (
        <Group title="Needs you" count={urgent.length} tone="gate">
          {urgent.map((c) => (
            <Row key={c.id} c={c} latest={latest[c.id]} expanded />
          ))}
        </Group>
      )}
      {groups.map((g) => (
        <Group key={g.title} title={g.title} count={g.items.length}>
          {g.items.map((c) => (
            <Row key={c.id} c={c} latest={latest[c.id]} />
          ))}
        </Group>
      ))}
    </div>
  );
};

const Group: FC<{ title: string; count: number; tone?: "gate"; children: React.ReactNode }> = ({ title, count, tone, children }) => (
  <section aria-label={title}>
    <h2 className={cn("label-mono mb-2 flex items-baseline gap-2 px-1", tone === "gate" ? "text-gate" : "text-quiet")}>
      {title}
      <span className="tabular-nums">{count}</span>
    </h2>
    {count > 0 ? <ul className="flex flex-col gap-2">{children}</ul> : <p className="px-1 text-sm text-quiet/60">Nobody here yet</p>}
  </section>
);

const Row: FC<{ c: Candidate; latest?: Latest; expanded?: boolean }> = ({ c, latest, expanded }) => {
  const selected = useJobStore((s) => s.candidateFilter === c.id);
  const setFilter = useJobStore((s) => s.setCandidateFilter);
  const openDrawer = useJobStore((s) => s.openDrawer);
  const stuck = c.needsHuman || c.state === "NEEDS_HUMAN";
  const waiting = !stuck && !!c.pendingApprovalId;
  const last = latest?.decision;

  return (
    <li
      id={`cand-${c.id}`}
      className={cn(
        "scroll-mt-4 rounded-2xl border bg-surface transition-colors",
        selected ? "border-ink/60 ring-1 ring-ink/10" : "border-line hover:border-ink/25",
        stuck && "border-l-[3px] border-l-danger",
        waiting && "border-l-[3px] border-l-gate",
      )}
    >
      <div className="flex items-center gap-3 px-3 py-2.5 sm:px-4">
        <button
          type="button"
          onClick={() => setFilter(selected ? null : c.id)}
          aria-pressed={selected}
          title={selected ? "Show all activity" : `Show only ${c.name}'s activity`}
          className="flex min-w-0 flex-1 items-center gap-3 text-left"
        >
          <CandidateAvatar name={c.name} size={38} />
          <span className="min-w-0 flex-1">
            <span className="block truncate font-medium text-ink">{c.name}</span>
            {c.headline && <span className="block truncate text-sm text-quiet">{c.headline}</span>}
            <span className={cn("block text-xs sm:hidden", stuck ? "text-danger" : waiting ? "text-gate" : "text-quiet")}>
              {waiting ? "Waiting for you" : STATE_LABEL[c.state]}
            </span>
            {!expanded && last && (
              <span className="block truncate text-xs text-quiet/90">
                <span aria-hidden className="text-agent">◆</span> {ACTION_LABEL[last.action] ?? last.action.replaceAll("_", " ")}
                {last.reasoning && <> · {last.reasoning}</>}
              </span>
            )}
          </span>
        </button>
        <span
          className={cn(
            "hidden shrink-0 rounded-full px-2.5 py-0.5 text-xs font-medium sm:inline",
            stuck ? "bg-danger/10 text-danger" : waiting ? "bg-gate/10 text-gate" : c.state === "SHORTLISTED" ? "bg-ok/10 text-ok" : "bg-sunk text-quiet",
          )}
        >
          {waiting ? "Waiting for you" : STATE_LABEL[c.state]}
        </span>
        <span className="w-8 shrink-0 text-right font-mono text-sm tabular-nums text-ink" aria-label={c.score != null ? `Score ${c.score}` : undefined}>
          {c.score ?? <span className="text-quiet/50">–</span>}
        </span>
        <button
          type="button"
          onClick={() => openDrawer(c.id)}
          className="shrink-0 rounded-full border border-line px-3 py-1 text-sm text-ink transition hover:border-ink/40"
        >
          Details
        </button>
      </div>
      {expanded && (stuck || c.pendingApprovalId) && (
        <div className="border-t border-line px-3 py-3 sm:px-4">
          {stuck ? (
            <NeedsHumanCard
              args={{ candidateId: c.id, error: latest?.escalation?.error ?? "", traceUrl: latest?.escalation?.traceUrl ?? c.traceUrl }}
            />
          ) : (
            <ApprovalCard approvalId={c.pendingApprovalId!} candidateId={c.id} />
          )}
        </div>
      )}
    </li>
  );
};
