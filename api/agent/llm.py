"""Decide LLM: real Claude call or `LLM_MODE=stub` canned happy-path decisions."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel

from api.agent.prompts import build_decide_messages
from api.contracts import ActionName as A, Decision

DecideLLM = Callable[[list[A], Optional[str]], Awaitable[Decision]]

# Happy-path preference order for the stub: first offered action wins.
_STUB_ORDER = [
    A.search_candidates, A.enrich_candidate, A.find_contact, A.draft_outreach, A.send_outreach,
    A.process_reply, A.schedule_interview, A.process_interview, A.evaluate_candidate,
    A.shortlist, A.wait,
]


async def stub_llm(valid: list[A], error: Optional[str] = None) -> Decision:
    for a in _STUB_ORDER:
        if a in valid:
            return Decision(action=a, args={}, reasoning=f"stub: happy path -> {a.value}")
    return Decision(action=A.escalate if A.escalate in valid else valid[0], args={},
                    reasoning="stub: no happy-path action offered")


class _DecisionOut(BaseModel):
    """Wire format: args travel as a JSON string so the output schema stays strict."""
    action: A
    args_json: str = "{}"
    reasoning: str


def make_llm(mode: str, ctx: Any = None) -> DecideLLM:
    """`ctx` carries (job, candidate, memory, event) for the real prompt; unused by the stub."""
    if mode == "stub":
        return stub_llm

    from api.config import settings
    from api.tools.llm import LLM

    client = LLM(settings.anthropic_api_key, settings.model_decide)

    async def real(valid: list[A], error: Optional[str] = None) -> Decision:
        system, user = build_decide_messages(ctx.job, ctx.candidate, ctx.memory, ctx.event, valid, error)
        out = await client.structured(system=system, prompt=user, output=_DecisionOut)
        try:
            args = json.loads(out.args_json or "{}")
        except json.JSONDecodeError:
            args = {}
        return Decision(action=out.action, args=args if isinstance(args, dict) else {},
                        reasoning=out.reasoning)

    return real
