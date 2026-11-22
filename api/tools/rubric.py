"""RubricGenerator: role form -> weighted rubric (spec 02). Called by lane C's POST /jobs."""
from __future__ import annotations

from api.tools.llm import LLM, dumps
from api.tools.models import RoleForm, Rubric, RubricCriterion

SYSTEM = """You turn a startup CEO's role description into a hiring rubric.
Produce 4-6 criteria derived from the must-haves (heavier weights) and nice-to-haves (lighter weights).
Weights are decimals that sum to 1.0. Each description says what strong evidence looks like.
Also produce 4-6 interview_themes: topics for a 5-minute voice screen that would produce evidence for the criteria."""


class RubricGenerator:
    def __init__(self, llm: LLM | None):
        self._llm = llm

    async def generate(self, form: RoleForm | dict) -> Rubric:
        form = form if isinstance(form, RoleForm) else RoleForm.model_validate(form)
        if self._llm is None:
            return _stub(form)
        rubric = await self._llm.structured(system=SYSTEM, prompt=f"ROLE FORM:\n{dumps(form)}", output=Rubric)
        return normalize(rubric)


def normalize(r: Rubric) -> Rubric:
    total = sum(c.weight for c in r.criteria) or 1.0
    crits = [c.model_copy(update={"weight": round(c.weight / total, 3)}) for c in r.criteria]
    return r.model_copy(update={"criteria": crits})


def _stub(form: RoleForm) -> Rubric:
    crits = [RubricCriterion(name=s, weight=2, description=f"Hands-on, shipped work using {s}")
             for s in form.must_haves[:4]]
    crits += [RubricCriterion(name=s, weight=1, description=f"Some exposure to {s}")
              for s in form.nice_to_haves[: max(0, 5 - len(crits))]]
    if len(crits) < 4:
        crits.append(RubricCriterion(name="Communication", weight=1, description="Clear, concise answers"))
    themes = [f"Walk through a project using {c.name}" for c in crits[:3]] + ["Motivation for this role"]
    return normalize(Rubric(criteria=crits, interview_themes=themes))
