"""Drafter: outreach + replies (spec 02). WRITE (nothing leaves the system)."""
from __future__ import annotations

import difflib
from typing import Any

from api.tools.llm import LLM, UNTRUSTED_RULE, dumps, memory_for_prompt, to_jsonable, untrusted
from api.tools.models import Draft, Message

MAX_WORDS = 120
REDRAFT_MAX_SIMILARITY = 0.8

SYSTEM = f"""You write recruiting emails on behalf of a startup CEO.
Rules:
- Personal and specific: reference 1-2 concrete things from the candidate memory (a project, a skill, an employer). Never invent facts that aren't in the memory or the ROLE.
- Don't mention the company name, salary, team size, location policy or benefits unless they appear in ROLE.
- Short: under {MAX_WORDS} words. Plain text, no markdown, no emoji, no bracketed placeholders like [Name].
- One clear ask: a short voice screening call.
- End with exactly the SIGN-OFF given below.
- If CEO feedback is given, apply every point of it; the new draft must visibly differ from the previous one.
{UNTRUSTED_RULE}"""


def _words(text: str) -> int:
    return len(text.split())


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


class Drafter:
    def __init__(self, llm: LLM | None, company_name: str | None = None, ceo_name: str | None = None):
        self._llm = llm
        self._company = company_name
        self._signoff = f"Best,\n{ceo_name or 'the CEO'}" + (f", {company_name}" if company_name else "")

    def _role(self, job: Any) -> str:
        role = to_jsonable(job) or {}
        if isinstance(role, dict) and self._company:
            role = {**role, "company_name": self._company}
        return dumps(role)

    async def outreach(self, job: Any, memory: Any, feedback: str | None = None,
                       previous: Draft | None = None) -> Draft:
        if self._llm is None:
            return _stub_outreach(job, memory, feedback, self._signoff)
        prompt = (f"ROLE:\n{self._role(job)}\n\n"
                  f"{untrusted('candidate_memory', memory_for_prompt(memory))}\n\n"
                  f"SIGN-OFF (use verbatim):\n{self._signoff}\n")
        if previous:
            n = _words(previous.body)
            prompt += (f"\nPREVIOUS DRAFT ({n} words, rejected by the CEO):\n{dumps(previous)}\n"
                       f"\nCEO FEEDBACK (apply every point):\n{feedback or '(none given: make it clearly better)'}\n"
                       f"Rewrite the opening line and don't reuse sentences from the previous draft. "
                       f"If the feedback asks for shorter, use at most {max(25, int(n * 0.6))} words.\n")
        elif feedback:
            prompt += f"\nCEO FEEDBACK:\n{feedback}\n"
        prompt += "\nWrite the outreach email (subject + body)."

        draft = await self._llm.structured(system=SYSTEM, prompt=prompt, output=Draft)
        if previous and _similarity(draft.body, previous.body) > REDRAFT_MAX_SIMILARITY:
            draft = await self._llm.structured(
                system=SYSTEM, output=Draft,
                prompt=prompt + "\nYour last attempt was nearly identical to the previous draft. "
                                "Write a substantially different email.")
        return _ensure_subject(draft, job)

    async def reply(self, job: Any, memory: Any, thread: list[Message]) -> Draft:
        if self._llm is None:
            return Draft(subject=None, body="Thanks for the question! I'll make sure we cover that on a "
                         f"quick call. Would a 15-minute voice screen this week work?\n\n{self._signoff}")
        prompt = (f"ROLE:\n{self._role(job)}\n\n"
                  f"{untrusted('candidate_memory', memory_for_prompt(memory))}\n\n"
                  f"{untrusted('email_thread', thread)}\n\n"
                  f"SIGN-OFF (use verbatim):\n{self._signoff}\n\n"
                  "The candidate asked a question. Answer it only from facts in ROLE; if ROLE doesn't say, "
                  "say you'll cover it on the call. Then re-invite them to a short voice screen. "
                  "Subject may be null (it's a reply in the same thread).")
        return await self._llm.structured(system=SYSTEM, prompt=prompt, output=Draft)


def _ensure_subject(draft: Draft, job: Any) -> Draft:
    if draft.subject and draft.subject.strip():
        return draft
    j = to_jsonable(job)
    title = (j.get("title") if isinstance(j, dict) else None) or "a role"
    return draft.model_copy(update={"subject": f"{title}: quick chat?"})


def _stub_outreach(job: Any, memory: Any, feedback: str | None, signoff: str) -> Draft:
    j, m = to_jsonable(job) or {}, to_jsonable(memory) or {}
    title = j.get("title", "the role")
    profile = m.get("profile", m) or {}
    name = (profile.get("name") or "there").split()[0]
    projects = profile.get("projects") or []
    hook = f"your {projects[0]['name']} project" if projects else "your background"
    if feedback:
        body = f"Hi {name}, loved {hook}. We're hiring a {title}. 15-min voice chat this week?\n\n{signoff}"
    else:
        body = (f"Hi {name},\n\nI came across {hook} and was impressed. We're hiring a {title} "
                f"and your experience looks like a strong fit. Would you be open to a short voice "
                f"screening call this week?\n\n{signoff}")
    return Draft(subject=f"{title} role: quick chat?", body=body)
