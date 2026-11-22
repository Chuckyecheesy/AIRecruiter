"""Drafter: outreach + replies (spec 02). WRITE (nothing leaves the system)."""
from __future__ import annotations

from typing import Any

from api.tools.llm import LLM, dumps, to_jsonable
from api.tools.models import Draft, Message

SYSTEM = """You write recruiting emails on behalf of a startup CEO.
Rules:
- Personal and specific: reference 1-2 concrete things from the candidate's memory (a project, a skill). Never invent facts that aren't in the memory.
- Short: under 120 words for outreach. Plain text, no markdown, no emoji.
- One clear ask: a short voice screening call.
- Sign off as the CEO.
- If CEO feedback is given, follow it exactly; the new draft must visibly differ from the previous one."""


class Drafter:
    def __init__(self, llm: LLM | None):
        self._llm = llm

    async def outreach(self, job: Any, memory: Any, feedback: str | None = None,
                       previous: Draft | None = None) -> Draft:
        if self._llm is None:
            return _stub_outreach(job, memory, feedback)
        prompt = f"ROLE:\n{dumps(job)}\n\nCANDIDATE MEMORY:\n{dumps(memory)}\n"
        if previous:
            prompt += f"\nPREVIOUS DRAFT (rejected by the CEO):\n{dumps(previous)}\n"
        if feedback:
            prompt += f"\nCEO FEEDBACK ON THE PREVIOUS DRAFT:\n{feedback}\n"
        prompt += "\nWrite the outreach email (subject + body)."
        return await self._llm.structured(system=SYSTEM, prompt=prompt, output=Draft)

    async def reply(self, job: Any, memory: Any, thread: list[Message]) -> Draft:
        if self._llm is None:
            return Draft(subject=None, body="Great question! Happy to share more on a quick call. "
                         "The role is hybrid in Waterloo with flexible remote days. Best, the CEO")
        prompt = (f"ROLE:\n{dumps(job)}\n\nCANDIDATE MEMORY:\n{dumps(memory)}\n\n"
                  f"EMAIL THREAD (oldest first):\n{dumps(thread)}\n\n"
                  "The candidate asked a question. Answer it only from facts in the ROLE; if unknown, say "
                  "you'll confirm on the call. Then re-invite them to a short voice screen. Subject may be null.")
        return await self._llm.structured(system=SYSTEM, prompt=prompt, output=Draft)


def _stub_outreach(job: Any, memory: Any, feedback: str | None) -> Draft:
    j, m = to_jsonable(job) or {}, to_jsonable(memory) or {}
    title = j.get("title", "the role")
    profile = m.get("profile", m) or {}
    name = (profile.get("name") or "there").split()[0]
    projects = profile.get("projects") or []
    hook = f"your {projects[0]['name']} project" if projects else "your background"
    if feedback:
        body = f"Hi {name}, loved {hook}. We're hiring a {title}. 15-min voice chat this week? - CEO"
    else:
        body = (f"Hi {name},\n\nI came across {hook} and was impressed. We're hiring a {title} "
                f"and your experience looks like a strong fit. Would you be open to a short voice "
                f"screening call this week?\n\nBest,\nThe CEO")
    return Draft(subject=f"{title} role - quick chat?", body=body)
