"""Decide validation: the action must be in valid_actions; re-decide once, then escalate."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from api.agent.state_machine import valid_actions
from api.contracts import ActionName, CandidateState, Decision

LLMCall = Callable[[list[ActionName], Optional[str]], Awaitable[Decision]]


@dataclass
class DecideOutcome:
    decision: Optional[Decision]
    escalate: bool
    attempts: int
    error: Optional[str] = None


async def decide_validated(
    llm: LLMCall, state: CandidateState, job: Any, valid: Optional[list[ActionName]] = None,
    error: Optional[str] = None,
) -> DecideOutcome:
    """`valid` overrides the candidate table (job-level events have no candidate state).
    `error` is shown to the first LLM call (e.g. a tool rejected the previous decision's args)."""
    if valid is None:
        valid = valid_actions(state, job)
    if not valid:
        return DecideOutcome(None, False, 0, f"no valid actions in {getattr(state, 'value', state)}")
    for attempt in (1, 2):
        decision = await llm(list(valid), error)
        if decision.action in valid:
            return DecideOutcome(decision, False, attempt)
        error = (
            f"Action '{decision.action.value}' is not valid in state "
            f"{getattr(state, 'value', state)}. Choose one of: {', '.join(a.value for a in valid)}."
        )
    return DecideOutcome(None, True, 2, error)
