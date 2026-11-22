"""Candidate state machine (specs/01-state-machine.md).

The LLM only ever sees `valid_actions`; only this module applies transitions.
"""
from __future__ import annotations

from typing import Any, Optional

from api.contracts import ActionName as A, CandidateState as S

TERMINAL_STATES: frozenset[S] = frozenset({S.SHORTLISTED, S.REJECTED, S.DECLINED})

# Actions with no fixed result state are handled in apply_transition.
_TABLE: dict[S, dict[A, Optional[S]]] = {
    S.DISCOVERED: {A.enrich_candidate: S.RESEARCHED},
    S.RESEARCHED: {
        A.browser_research: S.RESEARCHED,
        A.find_contact: S.CONTACT_FOUND,
        A.reject_candidate_proposal: S.REJECTED,
    },
    S.CONTACT_FOUND: {A.draft_outreach: S.OUTREACH_DRAFTED},
    # send_outreach here only requests approval; the permission layer moves it on.
    S.OUTREACH_DRAFTED: {A.send_outreach: None, A.redraft_outreach: S.OUTREACH_DRAFTED},
    S.APPROVED: {A.send_outreach: S.CONTACTED},
    S.CONTACTED: {
        A.wait: S.CONTACTED,
        A.send_followup: S.CONTACTED,
        A.process_reply: None,  # depends on classification
        A.mark_stale: S.STALE,
    },
    S.INTERESTED: {A.schedule_interview: S.INTERVIEWING},
    S.INTERVIEWING: {A.wait: S.INTERVIEWING, A.process_interview: S.INTERVIEWED},
    S.INTERVIEWED: {A.evaluate_candidate: S.EVALUATED},
    S.EVALUATED: {A.shortlist: S.SHORTLISTED, A.reject_candidate_proposal: S.REJECTED},
    S.NEEDS_HUMAN: {A.resume: None},  # back to prev_state
    S.STALE: {A.reactivate: S.CONTACTED},
    S.SHORTLISTED: {},
    S.REJECTED: {},
    S.DECLINED: {},
}

_NO_ESCALATE = TERMINAL_STATES | {S.NEEDS_HUMAN, S.STALE}

_REPLY = {"interested": S.INTERESTED, "declined": S.DECLINED, "question": S.CONTACTED}


class InvalidTransition(ValueError):
    pass


def job_valid_actions(job: Any) -> list[A]:
    return [A.search_candidates]


def valid_actions(state: S, job: Any = None) -> list[A]:
    actions = list(_TABLE[S(state)])
    if state not in _NO_ESCALATE:
        actions.append(A.escalate)
    return actions


def apply_transition(state: S, action: A, result: Optional[dict] = None) -> S:
    state, action, result = S(state), A(action), result or {}
    if action not in valid_actions(state):
        raise InvalidTransition(f"{action.value} is not valid in state {state.value}")
    if action == A.escalate:
        return S.NEEDS_HUMAN
    if action == A.process_reply:
        cls = result.get("classification")
        if cls not in _REPLY:
            raise InvalidTransition(f"process_reply needs a valid classification, got {cls!r}")
        return _REPLY[cls]
    if action == A.resume:
        prev = result.get("prev_state")
        if prev is None:
            raise InvalidTransition("resume needs prev_state")
        return S(prev)
    nxt = _TABLE[state][action]
    if nxt is None:
        raise InvalidTransition(f"{action.value} in {state.value} has no direct result state")
    return nxt
