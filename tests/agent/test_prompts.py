"""Red tests for api/agent/prompts.py and LLM_MODE=stub (specs/05 "Decide prompt contract").

Assumed API:
    build_decide_messages(job, candidate, memory, event, valid_actions) -> (system: str, user: str)
    make_llm(mode) -> decide callable `async (valid_actions, error=None) -> Decision`
        (api/agent/llm.py); mode "stub" returns canned happy-path decisions offline.
"""
from types import SimpleNamespace

import pytest

from api.contracts import ActionName as A, CandidateMemory, CandidateState as S, Event, EventType as E

from api.agent import prompts

JOB = SimpleNamespace(title="Backend Eng", form={"must_haves": ["python"]}, rubric={"x": 1})
CAND = SimpleNamespace(name="Ada Lovelace", state=S.RESEARCHED)


def _msgs(valid=(A.find_contact, A.escalate), mem=None, event=None, error=None):
    mem = mem or CandidateMemory(candidate_id="c1", summary="Strong Python dev")
    event = event or Event(job_id="j", candidate_id="c1", type=E.CandidateEnriched, payload={"k": "v"})
    return prompts.build_decide_messages(JOB, CAND, mem, event, list(valid))


def test_system_prompt_states_role_and_rules():
    system, _ = _msgs()
    assert "recruiter" in system.lower()
    assert "valid_actions" in system or "valid actions" in system.lower()


def test_user_prompt_contains_all_sections():
    _, user = _msgs()
    for needle in ("Backend Eng", "Ada Lovelace", "RESEARCHED", "Strong Python dev",
                   "CandidateEnriched", "find_contact"):
        assert needle in user


def test_only_valid_actions_are_listed():
    _, user = _msgs(valid=[A.find_contact, A.escalate])
    assert "find_contact" in user and "escalate" in user
    assert "shortlist" not in user and "send_outreach" not in user


def test_recent_decisions_limited_to_last_five():
    mem = CandidateMemory(candidate_id="c1", decisions=[{"action": f"a{i}", "reasoning": f"r{i}"} for i in range(8)])
    _, user = _msgs(mem=mem)
    assert "r7" in user and "r3" in user
    assert "r2" not in user


def test_reply_text_and_rejection_feedback_reach_the_prompt():
    ev = Event(job_id="j", candidate_id="c1", type=E.EmailReplied, payload={"text": "Sure, tell me more!"})
    assert "Sure, tell me more!" in _msgs(event=ev)[1]
    ev = Event(job_id="j", candidate_id="c1", type=E.ApprovalRejected, payload={"feedback": "too pushy"})
    assert "too pushy" in _msgs(event=ev)[1]


def test_validation_error_is_appended_on_redecide():
    system, user = prompts.build_decide_messages(
        JOB, CAND, CandidateMemory(candidate_id="c1"),
        Event(job_id="j", type=E.Tick), [A.find_contact], error="Action 'shortlist' is not valid")
    assert "Action 'shortlist' is not valid" in user


async def test_stub_llm_returns_canned_happy_path():
    from api.agent import llm as llm_mod
    llm = llm_mod.make_llm("stub")
    dec = await llm([A.enrich_candidate, A.escalate], None)
    assert dec.action == A.enrich_candidate
    assert dec.reasoning


async def test_stub_llm_always_picks_from_offered_actions():
    from api.agent import llm as llm_mod
    llm = llm_mod.make_llm("stub")
    for valid in ([A.find_contact, A.browser_research, A.escalate], [A.draft_outreach, A.escalate]):
        assert (await llm(valid, None)).action in valid
