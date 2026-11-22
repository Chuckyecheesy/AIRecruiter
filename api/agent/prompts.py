"""Decide prompt contract (specs/05-agent-graph.md "Decide prompt contract")."""
from __future__ import annotations

import json
from typing import Any, Optional

from api.contracts import ActionName as A, CandidateMemory, Event

SYSTEM_PROMPT = (
    "You are a recruiter agent working for the CEO on the role below. "
    "Choose the single best next action for this candidate.\n"
    "Rules:\n"
    "- Pick only from the VALID ACTIONS list (valid_actions). Anything else is rejected.\n"
    "- Be concise in your reasoning.\n"
    "- Never invent facts that are not in the memory summary or the latest event.\n"
    "- Prefer the cheapest action that makes progress.\n"
    'Reply with JSON: {"action": ..., "args": {...}, "reasoning": ...}. '
    "Most actions need no args; supply args only for judgment calls (e.g. which URLs to browse)."
)

ACTION_HELP: dict[A, str] = {
    A.search_candidates: "find candidates matching the role's criteria",
    A.enrich_candidate: "fetch the full profile",
    A.browser_research: "browse the candidate's public links (args: urls)",
    A.find_contact: "look up a contact email",
    A.draft_outreach: "write the first outreach email",
    A.redraft_outreach: "rewrite the outreach draft using the CEO's feedback",
    A.send_outreach: "send the approved outreach email (needs CEO approval)",
    A.send_followup: "send one follow-up nudge (needs CEO approval)",
    A.draft_reply: "draft an answer to the candidate's question",
    A.process_reply: "classify the candidate's reply as interested / declined / question",
    A.mark_stale: "give up waiting for a reply",
    A.schedule_interview: "create the voice interview (needs CEO approval)",
    A.process_interview: "turn the interview transcript into notes",
    A.evaluate_candidate: "score the candidate against the rubric",
    A.shortlist: "shortlist the candidate",
    A.reject_candidate_proposal: "propose rejecting the candidate (needs CEO approval)",
    A.wait: "do nothing for now",
    A.escalate: "hand over to the CEO because you are unsure",
    A.resume: "resume from the previous state",
    A.reactivate: "reactivate a stale candidate",
}


def _val(x: Any) -> Any:
    return getattr(x, "value", x)


def build_decide_messages(
    job: Any, candidate: Any, memory: Optional[CandidateMemory], event: Event,
    valid_actions: list[A], error: Optional[str] = None,
) -> tuple[str, str]:
    form = getattr(job, "form", None) or {}
    rubric = getattr(job, "rubric", None) or {}
    parts = [f"JOB: {job.title}\nREQUIREMENTS: {json.dumps(form, default=str)}\nRUBRIC: {json.dumps(rubric, default=str)}"]
    if candidate is not None:
        parts.append(f"CANDIDATE: {candidate.name}, state={_val(candidate.state)}")
    if memory is not None:
        parts.append(f"MEMORY SUMMARY: {memory.summary or '(none yet)'}")
        recent = memory.decisions[-5:]
        lines = [f"  - {d.get('action')}: {d.get('reasoning', '')}" for d in recent]
        parts.append("RECENT DECISIONS:\n" + ("\n".join(lines) if lines else "  (none)"))
    parts.append(f"LATEST EVENT: {_val(event.type)} {json.dumps(event.payload, default=str)}")
    parts.append("VALID ACTIONS:\n" + "\n".join(
        f"  - {a.value}(): {ACTION_HELP.get(a, '')}" for a in valid_actions))
    if error:
        parts.append(f"YOUR PREVIOUS ANSWER WAS REJECTED: {error}")
    return SYSTEM_PROMPT, "\n\n".join(parts)
