"use client";

import { useEffect, useMemo, useRef, type FC } from "react";
import { buildFeed, type FeedItem } from "@/lib/activity";
import { useJobStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import { DecisionNote, DuplicateNote, StatusLine } from "./tool-uis";

const ACTION_NOUN: Record<string, string> = {
  send_outreach: "outreach email",
  send_followup: "follow-up",
  schedule_interview: "interview",
  reject_candidate_proposal: "rejection",
};

const scrollToCandidate = (id: string | null | undefined) => {
  if (id) document.getElementById(`cand-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
};

/** A one-line feed entry that jumps somewhere when clicked. */
const LinkLine: FC<{ icon: string; onClick: () => void; tone?: "gate" | "warn"; children: React.ReactNode }> = ({ icon, onClick, tone, children }) => (
  <button
    type="button"
    onClick={onClick}
    className={cn(
      "my-1 flex w-full items-baseline gap-2 text-left text-sm hover:underline hover:underline-offset-2",
      tone === "gate" ? "text-gate" : tone === "warn" ? "text-danger" : "text-ink",
    )}
  >
    <span aria-hidden className="w-4 shrink-0 text-center">{icon}</span>
    <span>{children}</span>
  </button>
);

const ApprovalLine: FC<{ args: Record<string, any>; action: string }> = ({ args, action }) => {
  const approval = useJobStore((s) => s.approvals[args.approvalId]);
  const name = args.candidateName ?? approval?.candidateName ?? "candidate";
  const pending = approval?.status === "pending";
  return (
    <LinkLine icon={pending ? "⏳" : "📝"} tone={pending ? "gate" : undefined} onClick={() => scrollToCandidate(args.candidateId)}>
      {name}: {ACTION_NOUN[action] ?? action} {pending ? "waiting for you" : "sent for approval"}
    </LinkLine>
  );
};

const Item: FC<{ item: FeedItem }> = ({ item }) => {
  const { toolName, args } = item;
  const nameOf = useJobStore((s) => s.candidates.find((c) => c.id === args.candidateId)?.name);
  const openDrawer = useJobStore((s) => s.openDrawer);
  const who = args.candidateName ?? nameOf ?? "candidate";

  switch (toolName) {
    case "decision":
      return <DecisionNote args={args as any} />;
    case "status":
      return <StatusLine args={args as any} />;
    case "duplicate":
      return <DuplicateNote args={args as any} />;
    case "feedback":
      return (
        <p className="my-1.5 ml-6 rounded-xl bg-sunk px-3 py-2 text-sm text-ink">
          <span className="text-quiet">
            You sent back {who}’s {ACTION_NOUN[args.action] ?? "draft"}:
          </span>{" "}
          “{args.feedback}”
        </p>
      );
    case "send_outreach":
    case "send_followup":
    case "schedule_interview":
    case "reject_candidate_proposal":
      return <ApprovalLine args={args} action={toolName} />;
    case "interview":
      return (
        <LinkLine icon="📞" onClick={() => openDrawer(args.candidateId)}>
          Interview link ready for {who}
        </LinkLine>
      );
    case "evaluate_candidate":
      return (
        <LinkLine icon="📊" onClick={() => openDrawer(args.candidateId)}>
          Evaluated {who}
          {args.score != null ? `: ${args.score}` : ""}
        </LinkLine>
      );
    case "escalated":
      return (
        <LinkLine icon="⚠️" tone="warn" onClick={() => scrollToCandidate(args.candidateId)}>
          {who} needs you{args.error ? `: ${args.error}` : ""}
        </LinkLine>
      );
    default:
      return null;
  }
};

export const ActivityFeed: FC = () => {
  const events = useJobStore((s) => s.events);
  const filter = useJobStore((s) => s.candidateFilter);
  const filteredName = useJobStore((s) => s.candidates.find((c) => c.id === s.candidateFilter)?.name);
  const setFilter = useJobStore((s) => s.setCandidateFilter);
  const items = useMemo(() => buildFeed(events, filter), [events, filter]);

  // Follow new activity unless the reader has scrolled up to look at something.
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  useEffect(() => {
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [items.length]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-2.5">
        <h2 className="label-mono text-quiet">Activity</h2>
        {filter && (
          <span className="flex items-center gap-1.5 rounded-full bg-sunk px-2.5 py-0.5 text-xs text-ink">
            Only {filteredName ?? "one candidate"}
            <button type="button" onClick={() => setFilter(null)} className="text-quiet hover:text-ink" aria-label="Show all activity">
              ✕
            </button>
          </span>
        )}
      </div>
      <div
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget;
          pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
        className="min-h-0 flex-1 overflow-y-auto px-4 py-3"
      >
        {items.length ? (
          items.map((it) => <Item key={it.id} item={it} />)
        ) : (
          <p className="text-sm text-quiet">Nothing yet. The agent’s decisions show up here as it works.</p>
        )}
      </div>
    </div>
  );
};
