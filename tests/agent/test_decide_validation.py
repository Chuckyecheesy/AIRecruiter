"""Red tests for decide validation (specs/01 "How the LLM is constrained", spec 05).

Assumed API in api/agent/decide.py:
    async decide_validated(llm, state, job) -> DecideOutcome
      llm: async callable (valid_actions: list[ActionName], error: str | None) -> Decision
      DecideOutcome: .decision (Decision | None), .escalate (bool), .attempts (int),
                     .error (str | None)
Behaviour: the LLM is only given valid_actions(state, job); an action outside that
list triggers exactly one re-decide with the validation error; a second invalid
action means escalate (decision None).
"""
from types import SimpleNamespace

from api.contracts import ActionName as A, CandidateState as S, Decision
from api.agent.decide import decide_validated
from api.agent.state_machine import valid_actions

JOB = SimpleNamespace(score_threshold=75, shortlist_target=3, max_candidates=5)


def _dec(action):
    return Decision(action=action, args={}, reasoning="because")


class ScriptedLLM:
    def __init__(self, *actions):
        self.actions = list(actions)
        self.calls = []  # (valid_actions, error)

    async def __call__(self, valid, error=None):
        self.calls.append((list(valid), error))
        return _dec(self.actions[len(self.calls) - 1])


async def test_llm_is_only_shown_valid_actions():
    llm = ScriptedLLM(A.find_contact)
    await decide_validated(llm, S.RESEARCHED, JOB)
    shown, _ = llm.calls[0]
    assert set(shown) == set(valid_actions(S.RESEARCHED, JOB))
    assert A.shortlist not in shown


async def test_valid_action_accepted_first_try():
    llm = ScriptedLLM(A.find_contact)
    out = await decide_validated(llm, S.RESEARCHED, JOB)
    assert out.decision.action == A.find_contact
    assert out.escalate is False
    assert out.attempts == 1
    assert len(llm.calls) == 1
    assert llm.calls[0][1] is None


async def test_invalid_then_valid_redecides_once_with_error():
    llm = ScriptedLLM(A.shortlist, A.find_contact)  # shortlist invalid in RESEARCHED
    out = await decide_validated(llm, S.RESEARCHED, JOB)
    assert out.decision.action == A.find_contact
    assert out.escalate is False
    assert out.attempts == 2
    assert len(llm.calls) == 2
    err = llm.calls[1][1]
    assert err and "shortlist" in err  # the validation error is appended to the retry


async def test_second_call_still_restricted_to_valid_actions():
    llm = ScriptedLLM(A.shortlist, A.find_contact)
    await decide_validated(llm, S.RESEARCHED, JOB)
    assert set(llm.calls[1][0]) == set(valid_actions(S.RESEARCHED, JOB))


async def test_invalid_twice_escalates_without_a_third_call():
    llm = ScriptedLLM(A.shortlist, A.send_outreach, A.find_contact)
    out = await decide_validated(llm, S.RESEARCHED, JOB)
    assert out.escalate is True
    assert out.decision is None
    assert out.attempts == 2
    assert len(llm.calls) == 2  # exactly one re-decide, never more
    assert out.error


async def test_escalate_is_a_legal_llm_choice_where_offered():
    llm = ScriptedLLM(A.escalate)
    out = await decide_validated(llm, S.CONTACTED, JOB)
    assert out.decision.action == A.escalate
    assert out.attempts == 1


async def test_terminal_state_never_calls_llm_and_escalates():
    llm = ScriptedLLM(A.wait)
    out = await decide_validated(llm, S.SHORTLISTED, JOB)
    assert llm.calls == []
    assert out.decision is None
