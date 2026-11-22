"use client";

import type { FC } from "react";
import { useJobStore } from "@/lib/store";
import { PIPELINE_COLUMNS, type Candidate } from "@/lib/types";
import { cn } from "@/lib/utils";
import { CandidateAvatar } from "./CandidateAvatar";

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

const Card: FC<{ c: Candidate }> = ({ c }) => {
  const filter = useJobStore((s) => s.candidateFilter);
  const setFilter = useJobStore((s) => s.setCandidateFilter);
  const openDrawer = useJobStore((s) => s.openDrawer);
  const active = filter === c.id;
  return (
    <li
      className={cn(
        "group rounded-xl border bg-surface px-3 py-2.5 text-sm transition-colors",
        active ? "border-ink/70 ring-1 ring-ink/10" : "border-line hover:border-ink/25",
        c.needsHuman && "border-l-[3px] border-l-danger",
        !c.needsHuman && c.pendingApprovalId && "border-l-[3px] border-l-gate",
      )}
    >
      <button
        type="button"
        onClick={() => setFilter(active ? null : c.id)}
        className="block w-full text-left"
        aria-pressed={active}
        title={active ? "Show everyone in the thread" : `Show only ${c.name} in the thread`}
      >
        <span className="flex gap-2.5">
          <CandidateAvatar name={c.name} size={34} className="mt-0.5" />
          <span className="block min-w-0 flex-1">
            <span className="flex items-baseline justify-between gap-2">
              <span className="truncate font-medium text-ink">{c.name}</span>
              {c.score != null && <span className="font-mono text-xs tabular-nums text-quiet">{c.score}</span>}
            </span>
            {c.headline && <span className="block truncate text-xs text-quiet">{c.headline}</span>}
            <span className="mt-1 flex items-center gap-1.5 text-xs">
              <span className={cn(c.needsHuman ? "text-danger" : c.pendingApprovalId ? "text-gate" : "text-quiet")}>
                {c.pendingApprovalId && !c.needsHuman ? "Waiting for you" : STATE_LABEL[c.state]}
              </span>
            </span>
          </span>
        </span>
      </button>
      <button
        type="button"
        onClick={() => openDrawer(c.id)}
        className="mt-1 ml-[44px] text-xs text-agent underline-offset-2 hover:underline"
      >
        Details
      </button>
    </li>
  );
};

export const Pipeline: FC = () => {
  const candidates = useJobStore((s) => s.candidates);
  const needsHuman = candidates.filter((c) => c.state === "NEEDS_HUMAN");
  const columns = PIPELINE_COLUMNS.map((col) => ({
    ...col,
    items: candidates.filter((c) => col.states.includes(c.state)),
  }));

  if (!candidates.length)
    return <p className="p-4 text-sm text-quiet">No candidates yet. They appear here as the agent finds them.</p>;

  return (
    <div className="flex flex-col gap-5 p-4">
      {needsHuman.length > 0 && <Column title="Needs you" items={needsHuman} tone="danger" />}
      {columns.map((col) => (
        <Column key={col.title} title={col.title} items={col.items} />
      ))}
    </div>
  );
};

const Column: FC<{ title: string; items: Candidate[]; tone?: "danger" }> = ({ title, items, tone }) => (
  <section aria-label={title}>
    <h2 className={cn("label-mono mb-2 flex items-baseline justify-between px-0.5", tone === "danger" ? "text-danger" : "text-quiet")}>
      {title}
      <span className="tabular-nums">{items.length}</span>
    </h2>
    {items.length > 0 ? (
      <ul className="flex flex-col gap-1.5">
        {items.map((c) => (
          <Card key={c.id} c={c} />
        ))}
      </ul>
    ) : (
      <p className="px-0.5 text-xs text-quiet/60">Nobody here yet</p>
    )}
  </section>
);
