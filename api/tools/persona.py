"""CandidatePersona: an LLM plays the candidate and replies to outreach (spec 02). Internal.

Pure: returns reply text. Lane A schedules the delayed EmailReplied event (events.not_before).
"""
from __future__ import annotations

import random

from pydantic import BaseModel

from api.tools.llm import LLM, dumps
from api.tools.models import Disposition, EnrichedProfile, Message, ReplyClassification

class _Body(BaseModel):
    body: str


DISPOSITION_WEIGHTS: dict[Disposition, float] = {
    "interested": 0.6, "not_interested": 0.2, "asks_question": 0.1, "no_reply": 0.1,
}

INSTRUCTIONS: dict[str, str] = {
    "interested": "You're interested. Reply warmly, say you'd be happy to do a short call, and mention one thing about your own work that relates to the role.",
    "not_interested": "You're not interested (happy in your current role). Decline politely in 1-2 sentences.",
    "asks_question": "You're curious but not committed. Ask exactly one specific question (e.g. remote policy, team size, or the tech stack) and don't agree to a call yet.",
}

SYSTEM = """You are role-playing a job candidate replying to a recruiting email, for a demo.
Write ONLY the email body, plain text, 2-5 sentences, in the candidate's voice, consistent with their profile. No subject line."""

STUBS = {
    "interested": "Hi! Thanks for reaching out, this sounds really interesting. I'd be happy to hop on a quick call this week.",
    "not_interested": "Thanks for thinking of me, but I'm happy where I am right now. Best of luck with the search!",
    "asks_question": "Thanks for the note! Before we chat, is the role remote-friendly?",
}


def pick_disposition(rng: random.Random | None = None) -> Disposition:
    """Weighted 60/20/10/10. Lane A calls this when candidate.demo_disposition is not pinned."""
    rng = rng or random
    return rng.choices(list(DISPOSITION_WEIGHTS), weights=list(DISPOSITION_WEIGHTS.values()))[0]


class LLMCandidatePersona:
    def __init__(self, llm: LLM | None):
        self._llm = llm

    async def reply(self, profile: EnrichedProfile, thread: list[Message], disposition: Disposition) -> str:
        if disposition == "no_reply":
            raise ValueError("no_reply produces no message; lane A should not call reply()")
        if self._llm is None:
            return STUBS[disposition]
        prompt = (f"YOUR PROFILE:\n{dumps(profile)}\n\nTHE THREAD SO FAR (oldest first):\n{dumps(thread)}\n\n"
                  f"HOW TO RESPOND: {INSTRUCTIONS[disposition]}")
        out = await self._llm.structured(system=SYSTEM, prompt=prompt, output=_Body, max_tokens=1000)
        return out.body


CLASSIFY_SYSTEM = """Classify a candidate's email reply to recruiting outreach:
- interested: agrees to talk / wants to proceed
- declined: not interested
- question: asks something before committing"""


class ReplyClassifier:
    """Optional helper for lane A's process_reply (they may classify inside decide instead)."""

    def __init__(self, llm: LLM | None):
        self._llm = llm

    async def classify(self, reply_text: str) -> ReplyClassification:
        if self._llm is None:
            t = reply_text.lower()
            intent = "question" if "?" in t else ("declined" if any(w in t for w in ("not interested", "happy where", "pass")) else "interested")
            return ReplyClassification(intent=intent, reasoning="(stub) keyword match")
        return await self._llm.structured(system=CLASSIFY_SYSTEM, prompt=f"REPLY:\n{reply_text}",
                                          output=ReplyClassification, max_tokens=500)
