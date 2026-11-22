import type { EventRow } from "./types";

/**
 * Events -> activity feed items.
 *
 * Each event becomes zero or more items. `toolName` picks the renderer in
 * components/ActivityFeed.tsx:
 *   decision · status · duplicate · feedback · send_outreach · send_followup · schedule_interview ·
 *   reject_candidate_proposal · interview · evaluate_candidate · escalated
 */

export type FeedItem = { id: string; toolName: string; args: Record<string, any> };
type Part = FeedItem;

const tool = (id: string, toolName: string, args: Record<string, unknown>): Part => ({ id, toolName, args });

export const ACTION_LABEL: Record<string, string> = {
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
        parts.push(tool(`ev${ev.id}-a`, action, { approvalId: ev.approvalId, candidateId: ev.candidateId, candidateName: ev.candidateName }));
        return parts; // reasoning is shown inside the inline card
      }
      break;
    case "ApprovalGranted":
      parts.push(
        status(ev, "✅", `Approved ${String(p.action ?? "").replaceAll("_", " ")} for ${who}${p.decided_by === "auto" ? " (auto)" : ""}.`),
      );
      break;
    case "ApprovalRejected":
      // The CEO feedback line is added in buildFeed; nothing on the agent side.
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
      if (dec && dec.args.action === "shortlist") {
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

export function buildFeed(events: EventRow[], candidateFilter: string | null): FeedItem[] {
  const approvalRequestedFor = new Set<string>();
  for (const ev of events)
    if (ev.type === "ApprovalRequested" && ev.payload?.action) approvalRequestedFor.add(`${ev.candidateId}:${ev.payload.action}`);

  const visible = candidateFilter ? events.filter((e) => e.candidateId === candidateFilter) : events;
  const out: FeedItem[] = [];
  for (const ev of visible) {
    if (ev.type === "ApprovalRejected" && ev.payload?.feedback) {
      out.push(
        tool(`ev${ev.id}-fb`, "feedback", {
          candidateId: ev.candidateId,
          candidateName: ev.candidateName,
          action: ev.payload.action ?? null,
          feedback: ev.payload.feedback,
        }),
      );
    }
    out.push(...eventParts(ev, { approvalRequestedFor }));
  }
  return out;
}
