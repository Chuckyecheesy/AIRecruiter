"""Red tests for api/agent/state_machine.py, derived from specs/01-state-machine.md.

Assumed API (implement to match, or tell me what to change):
    valid_actions(state: CandidateState, job) -> list[ActionName]
    job_valid_actions(job) -> list[ActionName]            # the "(job)" row
    apply_transition(state, action, result=None) -> CandidateState
    InvalidTransition(ValueError)                          # raised on an illegal pair
    TERMINAL_STATES: frozenset[CandidateState]
`result` is a dict. process_reply reads result["classification"] in
{"interested", "declined", "question"}; resume reads result["prev_state"].
"""
from types import SimpleNamespace

import pytest

from api.contracts import ActionName as A, CandidateState as S
from api.agent.state_machine import (
    TERMINAL_STATES,
    InvalidTransition,
    apply_transition,
    job_valid_actions,
    valid_actions,
)

JOB = SimpleNamespace(score_threshold=75, shortlist_target=3, max_candidates=5)

# state -> exact set of actions the LLM may see (spec 01 transition table)
EXPECTED_VALID = {
    S.DISCOVERED: {A.enrich_candidate, A.escalate},
    S.RESEARCHED: {A.browser_research, A.find_contact, A.reject_candidate_proposal, A.escalate},
    S.CONTACT_FOUND: {A.draft_outreach, A.escalate},
    S.OUTREACH_DRAFTED: {A.send_outreach, A.redraft_outreach, A.escalate},
    S.APPROVED: {A.send_outreach, A.escalate},
    S.CONTACTED: {A.wait, A.send_followup, A.process_reply, A.mark_stale, A.escalate},
    S.INTERESTED: {A.schedule_interview, A.escalate},
    S.INTERVIEWING: {A.wait, A.process_interview, A.escalate},
    S.INTERVIEWED: {A.evaluate_candidate, A.escalate},
    S.EVALUATED: {A.shortlist, A.reject_candidate_proposal, A.escalate},
    S.NEEDS_HUMAN: {A.resume},
    S.STALE: {A.reactivate},
    S.SHORTLISTED: set(),
    S.REJECTED: set(),
    S.DECLINED: set(),
}

# (state, action, result, expected next state): every transition row in the table
TRANSITIONS = [
    (S.DISCOVERED, A.enrich_candidate, None, S.RESEARCHED),
    (S.RESEARCHED, A.browser_research, None, S.RESEARCHED),
    (S.RESEARCHED, A.find_contact, None, S.CONTACT_FOUND),
    (S.RESEARCHED, A.reject_candidate_proposal, None, S.REJECTED),
    (S.CONTACT_FOUND, A.draft_outreach, None, S.OUTREACH_DRAFTED),
    (S.OUTREACH_DRAFTED, A.redraft_outreach, None, S.OUTREACH_DRAFTED),
    (S.APPROVED, A.send_outreach, None, S.CONTACTED),
    (S.CONTACTED, A.wait, None, S.CONTACTED),
    (S.CONTACTED, A.send_followup, None, S.CONTACTED),
    (S.CONTACTED, A.process_reply, {"classification": "interested"}, S.INTERESTED),
    (S.CONTACTED, A.process_reply, {"classification": "declined"}, S.DECLINED),
    (S.CONTACTED, A.process_reply, {"classification": "question"}, S.CONTACTED),
    (S.CONTACTED, A.mark_stale, None, S.STALE),
    (S.INTERESTED, A.schedule_interview, None, S.INTERVIEWING),
    (S.INTERVIEWING, A.wait, None, S.INTERVIEWING),
    (S.INTERVIEWING, A.process_interview, None, S.INTERVIEWED),
    (S.INTERVIEWED, A.evaluate_candidate, None, S.EVALUATED),
    (S.EVALUATED, A.shortlist, None, S.SHORTLISTED),
    (S.EVALUATED, A.reject_candidate_proposal, None, S.REJECTED),
    (S.STALE, A.reactivate, None, S.CONTACTED),
]


def _ids(rows):
    return [f"{s.value}-{a.value}" for s, a, *_ in rows]


# ---------- job-level row ----------

def test_job_level_only_allows_search_candidates():
    assert list(job_valid_actions(JOB)) == [A.search_candidates]


# ---------- valid_actions restricts the LLM ----------

def test_expected_table_covers_every_candidate_state():
    assert set(EXPECTED_VALID) == set(S)


@pytest.mark.parametrize("state", list(S), ids=lambda s: s.value)
def test_valid_actions_exact_set(state):
    got = valid_actions(state, JOB)
    assert set(got) == EXPECTED_VALID[state]
    assert len(got) == len(set(got)), "no duplicate actions"


@pytest.mark.parametrize("state", list(S), ids=lambda s: s.value)
def test_valid_actions_returns_action_names(state):
    assert all(isinstance(a, A) for a in valid_actions(state, JOB))


def test_terminal_states_offer_nothing():
    assert TERMINAL_STATES == {S.SHORTLISTED, S.REJECTED, S.DECLINED}
    for s in TERMINAL_STATES:
        assert valid_actions(s, JOB) == []


def test_stale_is_not_terminal_but_only_ceo_reactivate():
    assert S.STALE not in TERMINAL_STATES
    assert set(valid_actions(S.STALE, JOB)) == {A.reactivate}


def test_escalate_offered_in_every_non_terminal_state_except_paused_ones():
    for s in S:
        if s in TERMINAL_STATES or s in (S.NEEDS_HUMAN, S.STALE):
            assert A.escalate not in valid_actions(s, JOB)
        else:
            assert A.escalate in valid_actions(s, JOB)


def test_needs_human_only_offers_resume():
    assert set(valid_actions(S.NEEDS_HUMAN, JOB)) == {A.resume}


def test_valid_actions_does_not_mutate_between_calls():
    first = valid_actions(S.CONTACTED, JOB)
    first.append(A.shortlist)  # caller mutation must not leak
    assert A.shortlist not in valid_actions(S.CONTACTED, JOB)


# ---------- apply_transition: one test per table row ----------

@pytest.mark.parametrize("state,action,result,expected", TRANSITIONS, ids=_ids(TRANSITIONS))
def test_apply_transition_table_row(state, action, result, expected):
    assert apply_transition(state, action, result) == expected


def test_resume_returns_previous_state():
    for prev in (S.RESEARCHED, S.CONTACTED, S.INTERVIEWING, S.EVALUATED):
        assert apply_transition(S.NEEDS_HUMAN, A.resume, {"prev_state": prev}) == prev


def test_resume_without_prev_state_is_invalid():
    with pytest.raises(InvalidTransition):
        apply_transition(S.NEEDS_HUMAN, A.resume, {})


@pytest.mark.parametrize(
    "state", [s for s in S if s not in TERMINAL_STATES and s not in (S.NEEDS_HUMAN, S.STALE)],
    ids=lambda s: s.value,
)
def test_escalate_from_any_active_state_goes_to_needs_human(state):
    assert apply_transition(state, A.escalate, None) == S.NEEDS_HUMAN


def test_process_reply_requires_a_classification():
    with pytest.raises(InvalidTransition):
        apply_transition(S.CONTACTED, A.process_reply, {})
    with pytest.raises(InvalidTransition):
        apply_transition(S.CONTACTED, A.process_reply, {"classification": "bogus"})


# ---------- invalid transitions ----------

def test_invalid_transition_is_a_value_error():
    assert issubclass(InvalidTransition, ValueError)


@pytest.mark.parametrize("state", list(S), ids=lambda s: s.value)
def test_every_action_outside_valid_actions_is_rejected(state):
    allowed = set(valid_actions(state, JOB))
    for action in A:
        if action in allowed:
            continue
        with pytest.raises(InvalidTransition):
            apply_transition(state, action, {"classification": "interested", "prev_state": S.CONTACTED})


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=lambda s: s.value), ids=lambda s: s.value)
def test_terminal_states_reject_every_action(state):
    for action in A:
        with pytest.raises(InvalidTransition):
            apply_transition(state, action, None)


def test_cannot_skip_states():
    with pytest.raises(InvalidTransition):
        apply_transition(S.DISCOVERED, A.draft_outreach, None)
    with pytest.raises(InvalidTransition):
        apply_transition(S.OUTREACH_DRAFTED, A.schedule_interview, None)
    with pytest.raises(InvalidTransition):
        apply_transition(S.CONTACTED, A.shortlist, None)


def test_search_candidates_is_not_a_candidate_action():
    for s in S:
        assert A.search_candidates not in valid_actions(s, JOB)
