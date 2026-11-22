"""RubricGenerator: role form -> weighted rubric (spec 02). Called by lane C's POST /jobs."""
from __future__ import annotations

from api.tools.llm import LLM, dumps
from api.tools.models import RoleForm, Rubric, RubricCriterion

SYSTEM = """You turn a startup CEO's role description into a hiring rubric.
Produce 4-6 criteria derived from the must-haves (heavier weights) and nice-to-haves (lighter weights).
Weights are decimals that sum to 1.0. Each description says what strong evidence looks like.
Also produce 4-6 interview_themes: topics for a 5-minute voice screen that would produce evidence for the criteria."""


MIN_CRITERIA, MAX_CRITERIA = 4, 6
_GENERIC = [
    ("Communication", "Clear, concise answers; explains trade-offs"),
    ("Ownership", "Shipped things end to end; takes responsibility for outcomes"),
    ("Learning speed", "Picks up new tools quickly; concrete examples of ramping up"),
    ("Product sense", "Connects technical choices to user impact"),
]


class RubricGenerator:
    def __init__(self, llm: LLM | None):
        self._llm = llm

    async def generate(self, form: RoleForm | dict) -> Rubric:
        form = form if isinstance(form, RoleForm) else RoleForm.model_validate(form)
        if self._llm is None:
            return _stub(form)
        prompt = f"ROLE FORM:\n{dumps(form)}"
        rubric = await self._llm.structured(system=SYSTEM, prompt=prompt, output=Rubric)
        if not MIN_CRITERIA <= len(rubric.criteria) <= MAX_CRITERIA:
            rubric = await self._llm.structured(
                system=SYSTEM, output=Rubric,
                prompt=prompt + f"\n\nYou returned {len(rubric.criteria)} criteria. Return {MIN_CRITERIA}-"
                                f"{MAX_CRITERIA}: merge related must-haves, or add role-relevant ones.")
        if len(rubric.criteria) > MAX_CRITERIA:
            top = sorted(rubric.criteria, key=lambda c: c.weight, reverse=True)[:MAX_CRITERIA]
            rubric = rubric.model_copy(update={"criteria": top})
        if len(rubric.interview_themes) > MAX_CRITERIA:
            rubric = rubric.model_copy(update={"interview_themes": rubric.interview_themes[:MAX_CRITERIA]})
        return normalize(rubric)


def normalize(r: Rubric) -> Rubric:
    """Weights sum to 1 (all-zero or negative weights fall back to equal weights)."""
    if not r.criteria:
        return r
    weights = [max(0.0, c.weight) for c in r.criteria]
    total = sum(weights)
    if total <= 0:
        weights, total = [1.0] * len(weights), float(len(weights))
    crits = [c.model_copy(update={"weight": round(w / total, 3)}) for c, w in zip(r.criteria, weights)]
    return r.model_copy(update={"criteria": crits})


def _stub(form: RoleForm) -> Rubric:
    crits = [RubricCriterion(name=s, weight=2, description=f"Hands-on, shipped work using {s}")
             for s in form.must_haves[:4]]
    crits += [RubricCriterion(name=s, weight=1, description=f"Some exposure to {s}")
              for s in form.nice_to_haves[: max(0, 5 - len(crits))]]
    for name, desc in _GENERIC:
        if len(crits) >= MIN_CRITERIA:
            break
        crits.append(RubricCriterion(name=name, weight=1, description=desc))
    themes = [f"Walk through a project using {c.name}" for c in crits[:3]] + ["Motivation for this role"]
    return normalize(Rubric(criteria=crits, interview_themes=themes))
