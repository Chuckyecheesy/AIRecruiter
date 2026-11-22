"""Evaluator: rubric-bound scoring (spec 02). WRITE."""
from __future__ import annotations

from typing import Any

from api.tools.llm import LLM, dumps, to_jsonable
from api.tools.models import CriterionScore, Evaluation

SYSTEM = """You evaluate a job candidate against a weighted rubric for a startup CEO.
Rules:
- Score each rubric criterion 0-100 using ONLY evidence in the candidate memory (profile, research notes, email thread, interview transcript).
- Every criterion needs 1-3 evidence items: short direct quotes or concrete facts from the memory. If there is no evidence, score low and say "no evidence".
- Overall score = weighted average of criterion scores, rounded to an integer.
- recommendation: "shortlist" if score >= the threshold given, "reject" if clearly below (more than 10 under), otherwise "borderline".
- summary: 2-3 sentences a busy CEO can read."""


class Evaluator:
    def __init__(self, llm: LLM | None):
        self._llm = llm

    async def evaluate(self, job: Any, memory: Any, threshold: int = 75) -> Evaluation:
        if self._llm is None:
            return _stub(job)
        prompt = (f"ROLE + RUBRIC:\n{dumps(job)}\n\nSHORTLIST THRESHOLD: {threshold}\n\n"
                  f"CANDIDATE MEMORY:\n{dumps(memory)}\n\nEvaluate the candidate.")
        ev = await self._llm.structured(system=SYSTEM, prompt=prompt, output=Evaluation,
                                        max_tokens=8000, effort="medium")
        return _recompute(ev, threshold)


def _recompute(ev: Evaluation, threshold: int) -> Evaluation:
    """Don't trust the model's arithmetic: recompute the weighted score + recommendation."""
    total_w = sum(c.weight for c in ev.criteria) or 1.0
    score = round(sum(c.score * c.weight for c in ev.criteria) / total_w)
    rec = "shortlist" if score >= threshold else ("reject" if score < threshold - 10 else "borderline")
    return ev.model_copy(update={"score": score, "recommendation": rec})


def _stub(job: Any) -> Evaluation:
    rubric = (to_jsonable(job) or {}).get("rubric") or {}
    crits = rubric.get("criteria") or [{"name": "Overall fit", "weight": 1.0}]
    scores = [CriterionScore(name=c["name"], weight=c["weight"], score=85,
                             evidence=["(stub) evidence from memory"]) for c in crits]
    return _recompute(Evaluation(score=0, criteria=scores, recommendation="borderline",
                                 summary="(stub evaluation) Strong overall fit."), 75)
