"use client";

import { useExternalStoreRuntime, type AppendMessage, type ThreadMessageLike } from "@assistant-ui/react";
import { useMemo, useState } from "react";
import { api } from "./api";
import { useJobStore, type LocalChat } from "./store";
import type { EventRow } from "./types";

/**
 * Events -> assistant-ui messages.
 *
 * Each event becomes one or more tool-call parts. Tool names here are UI names
 * (see components/tool-uis.tsx), named after the action where one exists:
 *   decision · status · duplicate · send_outreach · send_followup · schedule_interview ·
 *   reject_candidate_proposal · interview · evaluate_candidate · escalated
 * CEO feedback on a rejected approval becomes a user message, so the thread reads as a
 * conversation. Consecutive agent parts are grouped into one assistant message.
 */

type Part = Extract<ThreadMessageLike["content"], readonly unknown[]>[number];

const tool = (id: string, toolName: string, args: Record<string, unknown>): Part => ({
  type: "tool-call",
  toolCallId: id,
  toolName,
  args: args as any,
  result: { ok: true },
});

const ACTION_LABEL: Record<string, string> = {
  search_candidates: "searched for candidates",
  enrich_candidate: "enriched the profile",
  browser_research: "researched public pages",
  find_contact: "found contact details",
  draft_outreach: "drafted outreach",
  redraft_outreach: "redrafted outreach",
  draft_reply: "drafted a reply",
  process_reply: "read the reply",
  process_interview: "processed the interview",
  evaluate_candidate: "evaluated the candidate",
  shortlist: "shortlisted",
  mark_stale: "marked stale",
  wait: "is waiting",
  escalate: "escalated to you",
};

function decisionPart(ev: EventRow, key = "d"): Part | null {
  const d = ev.payload?.decision;
  if (!d?.action) return null;
  return tool(`ev${ev.id}-${key}`, "decision", {
    eventId: ev.id,
    eventType: ev.type,
    candidateId: ev.candidateId,
    candidateName: ev.candidateName,
    action: d.action,
    label: ACTION_LABEL[d.action] ?? d.action,
    reasoning: d.reasoning ?? "",
  });
}

const status = (ev: EventRow, icon: string, text: string, extra: Record<string, unknown> = {}): Part =>
  tool(`ev${ev.id}-s`, "status", { icon, text, candidateId: ev.candidateId, eventType: ev.type, ...extra });

export function eventParts(ev: EventRow, ctx: { approvalRequestedFor: Set<string> }): Part[] {
  const p = ev.payload ?? {};
  const who = ev.candidateName ?? "candidate";
  const parts: Part[] = [];
  const dec = decisionPart(ev);

  switch (ev.type) {
    case "JobCreated":
      parts.push(status(ev, "🚀", `Recruiter started${p.title ? ` for ${p.title}` : ""}.`));
      break;
    case "CandidateFound":
      parts.push(status(ev, "🔎", `Found ${who}${p.headline ? `, ${p.headline}` : ""}.`));
      break;
    case "ApprovalRequested":
      if (ev.approvalId) {
        const action = p.action ?? "send_outreach";
        parts.push(tool(`ev${ev.id}-a`, action, { approvalId: ev.approvalId, candidateId: ev.candidateId }));
        return parts; // reasoning is shown inside the card
      }
      break;
    case "ApprovalGranted":
      parts.push(
        status(ev, "✅", `Approved ${String(p.action ?? "").replaceAll("_", " ")} for ${who}${p.decided_by === "auto" ? " (auto)" : ""}.`),
      );
      break;
    case "ApprovalRejected":
      // Rendered as a CEO user message elsewhere; nothing on the agent side.
      return dec ? [dec] : [];
    case "EmailSent":
      parts.push(status(ev, "📧", `Sent to ${who} (simulated)${p.subject ? `: “${p.subject}”` : ""}.`));
      break;
    case "EmailReplied":
      parts.push(status(ev, "💬", `${who} replied: “${truncate(p.body ?? p.text ?? "", 180)}”`));
      break;
    case "InterviewScheduled":
      if (ctx.approvalRequestedFor.has(`${ev.candidateId}:schedule_interview`)) {
        parts.push(status(ev, "📞", `Interview link ready for ${who}.`));
      } else {
        parts.push(tool(`ev${ev.id}-i`, "interview", { candidateId: ev.candidateId }));
      }
      break;
    case "InterviewDone":
      parts.push(status(ev, "📞", `Interview with ${who} finished${p.duration_s ? ` (${fmtDuration(p.duration_s)})` : ""}.`));
      break;
    case "Evaluated":
      if (dec && (dec as any).args.action === "shortlist") {
        parts.push(status(ev, "⭐", `Shortlisted ${who}${p.score != null ? ` (${p.score})` : ""}.`));
      } else {
        parts.push(tool(`ev${ev.id}-e`, "evaluate_candidate", { candidateId: ev.candidateId, evaluationId: p.evaluation_id, score: p.score }));
      }
      break;
    case "ToolFailed":
      parts.push(status(ev, "⚠️", `${p.tool ?? "Tool"} failed for ${who}${p.attempts ? ` after ${p.attempts} attempts` : ""}: ${p.error ?? p.error_kind ?? "error"}.`, { tone: "warn" }));
      break;
    case "Escalated":
      parts.push(tool(`ev${ev.id}-x`, "escalated", { candidateId: ev.candidateId, error: p.error ?? "", traceUrl: p.trace_url ?? null }));
      return parts;
    case "HumanResolved":
      parts.push(status(ev, "🙋", `${who}: ${p.resolution === "skip" ? "skipped by CEO" : `resumed${p.state ? ` at ${p.state}` : ""}`}.`));
      break;
    case "Tick":
      return [];
    default:
      break;
  }

  if (dec) parts.unshift(dec);

  const dups = p.duplicates_skipped ?? (p.duplicate_of ? [{ name: p.name ?? who, existing_candidate_id: p.duplicate_of }] : null);
  if (Array.isArray(dups)) {
    dups.forEach((d: any, i: number) =>
      parts.push(tool(`ev${ev.id}-dup${i}`, "duplicate", { name: d.name, existingCandidateId: d.existing_candidate_id, reason: d.reason ?? null })),
    );
  }
  return parts;
}

function truncate(s: string, n: number) {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

export function fmtDuration(s: number) {
  const m = Math.floor(s / 60);
  return m ? `${m}m ${s % 60}s` : `${s}s`;
}

type Item =
  | { kind: "event"; t: number; id: number; ev: EventRow }
  | { kind: "chat"; t: number; id: number; chat: LocalChat };

const GROUP_GAP_MS = 90_000;
const GROUP_MAX_PARTS = 14;

export function buildMessages(events: EventRow[], chat: LocalChat[], candidateFilter: string | null): ThreadMessageLike[] {
  const approvalRequestedFor = new Set<string>();
  for (const ev of events)
    if (ev.type === "ApprovalRequested" && ev.payload?.action) approvalRequestedFor.add(`${ev.candidateId}:${ev.payload.action}`);

  const visible = candidateFilter ? events.filter((e) => e.candidateId === candidateFilter) : events;
  const items: Item[] = [
    ...visible.map((ev) => ({ kind: "event" as const, t: Date.parse(ev.createdAt), id: ev.id, ev })),
    ...(candidateFilter ? [] : chat).map((c, i) => ({ kind: "chat" as const, t: Date.parse(c.createdAt), id: i, chat: c })),
  ];
  items.sort((a, b) => a.t - b.t || (a.kind === b.kind ? a.id - b.id : a.kind === "event" ? -1 : 1));

  const out: ThreadMessageLike[] = [];
  let group: { id: string; t: number; last: number; parts: Part[] } | null = null;
  const flush = () => {
    if (group?.parts.length)
      out.push({
        role: "assistant",
        id: group.id,
        createdAt: new Date(group.t),
        content: group.parts,
        status: { type: "complete", reason: "stop" },
      });
    group = null;
  };

  for (const it of items) {
    if (it.kind === "chat") {
      flush();
      out.push({ role: it.chat.role, id: it.chat.id, createdAt: new Date(it.t), content: it.chat.text,
        ...(it.chat.role === "assistant" ? { status: { type: "complete", reason: "stop" } as const } : {}) });
      continue;
    }
    const ev = it.ev;
    if (ev.type === "ApprovalRejected" && ev.payload?.feedback) {
      flush();
      out.push({
        role: "user",
        id: `ev-${ev.id}-fb`,
        createdAt: new Date(it.t),
        content: ev.payload.feedback,
        metadata: { custom: { approvalId: ev.approvalId, candidateName: ev.candidateName, action: ev.payload.action } },
      });
    }
    const parts = eventParts(ev, { approvalRequestedFor });
    if (!parts.length) continue;
    const cur = group as { id: string; t: number; last: number; parts: Part[] } | null;
    if (!cur || it.t - cur.last > GROUP_GAP_MS || cur.parts.length >= GROUP_MAX_PARTS) {
      flush();
      group = { id: `ev-${ev.id}`, t: it.t, last: it.t, parts: [] };
    }
    group!.parts.push(...parts);
    group!.last = it.t;
  }
  flush();
  return out;
}

function textOf(m: AppendMessage) {
  return m.content
    .map((p) => (p.type === "text" ? p.text : ""))
    .join("\n")
    .trim();
}

export function useRecruiterRuntime() {
  const events = useJobStore((s) => s.events);
  const chat = useJobStore((s) => s.chat);
  const candidateFilter = useJobStore((s) => s.candidateFilter);
  const [isRunning, setRunning] = useState(false);

  const messages = useMemo(() => buildMessages(events, chat, candidateFilter), [events, chat, candidateFilter]);

  return useExternalStoreRuntime<ThreadMessageLike>({
    messages,
    convertMessage: (m) => m,
    isRunning,
    onNew: async (m) => {
      const text = textOf(m);
      if (!text) return;
      const { jobId, focusedApprovalId, candidateFilter, addChat, refreshApprovals, refreshJob } = useJobStore.getState();
      if (!jobId) return;
      setRunning(true);
      try {
        const res = await api.chat(jobId, { text, approvalId: focusedApprovalId, candidateId: candidateFilter });
        // A rejection shows up as the CEO message via the ApprovalRejected event; avoid a duplicate.
        if (res.actionTaken !== "reject") addChat({ role: "user", text });
        addChat({ role: "assistant", text: res.reply });
        useJobStore.getState().setFocusedApproval(null);
        refreshApprovals();
        refreshJob();
      } catch (e) {
        addChat({ role: "user", text });
        addChat({ role: "assistant", text: `⚠️ ${e instanceof Error ? e.message : String(e)}` });
      } finally {
        setRunning(false);
      }
    },
  });
}
