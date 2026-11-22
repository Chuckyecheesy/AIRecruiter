"""Tool I/O models (spec 02). Lane B owns these; re-export them from api/contracts.py.

Adapters take and return only these models. They never touch the DB.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# ---------- Ploid (discovery + enrichment) ----------


class SearchCriteria(BaseModel):
    title: str
    must_have_skills: list[str] = []
    nice_to_have_skills: list[str] = []
    location: str | None = None
    seniority: str | None = None
    free_text: str | None = None


class PloidCandidate(BaseModel):
    ploid_id: str
    name: str
    headline: str | None = None
    location: str | None = None
    profile_urls: list[str] = []


class Project(BaseModel):
    name: str
    description: str | None = None
    url: str | None = None
    tech: list[str] = []


class Experience(BaseModel):
    company: str
    title: str
    start: str | None = None
    end: str | None = None
    summary: str | None = None


class EnrichedProfile(BaseModel):
    ploid_id: str
    name: str
    headline: str | None = None
    location: str | None = None
    skills: list[str] = []
    projects: list[Project] = []
    education: list[str] = []
    experience: list[Experience] = []
    links: dict[str, str] = {}  # github, portfolio, linkedin
    raw: dict = {}
    completeness: float = Field(ge=0, le=1)  # < 0.6 -> browser_research candidate


# ---------- Fiber (contact lookup) ----------


class ContactInfo(BaseModel):
    email: str | None = None
    phone: str | None = None
    confidence: float = 0.0
    source: str


# ---------- Browser (READ-only research) ----------


class PageSummary(BaseModel):
    url: str
    title: str | None = None
    extracted_text: str = ""  # truncated to 8k chars


class BrowserFindings(BaseModel):
    pages: list[PageSummary] = []
    extracted_skills: list[str] = []
    notable_projects: list[str] = []


# ---------- Messages (email sim + persona) ----------


class Message(BaseModel):
    """One message in a candidate thread, as passed to the drafter/persona."""

    direction: Literal["out", "in"]
    subject: str | None = None
    body: str
    created_at: datetime | None = None


class SendReceipt(BaseModel):
    message_id: str
    to: str
    thread_id: str
    simulated: bool = True
    sent_at: datetime


Disposition = Literal["interested", "not_interested", "asks_question", "no_reply"]


# ---------- ElevenLabs (voice interview) ----------


class InterviewSession(BaseModel):
    conversation_id: str | None = None
    join_url: str
    expires_at: datetime


class Turn(BaseModel):
    role: Literal["agent", "candidate"]
    text: str
    t_s: float | None = None


class InterviewTranscript(BaseModel):
    conversation_id: str
    turns: list[Turn]
    duration_s: int
    ended_at: datetime


# ---------- Claude tools (drafter / evaluator / rubric) ----------


class Draft(BaseModel):
    subject: str | None = None  # None for a plain reply in-thread
    body: str


class RubricCriterion(BaseModel):
    name: str
    weight: float = Field(description="0-1; all weights sum to 1")
    description: str


class Rubric(BaseModel):
    criteria: list[RubricCriterion]
    interview_themes: list[str]


class RoleForm(BaseModel):
    title: str
    must_haves: list[str] = []
    nice_to_haves: list[str] = []
    location: str | None = None
    seniority: str | None = None
    free_text: str | None = None


class CriterionScore(BaseModel):
    name: str
    weight: float
    score: int = Field(ge=0, le=100)
    evidence: list[str] = Field(description="Quotes or facts taken from the candidate memory")


class Evaluation(BaseModel):
    score: int = Field(ge=0, le=100)
    criteria: list[CriterionScore]
    recommendation: Literal["shortlist", "reject", "borderline"]
    summary: str


# Used by lane A's process_reply if they want it; lane B provides the classifier.
# (No docstring: Pydantic sends it to the model as the schema description.)
class ReplyClassification(BaseModel):

    intent: Literal["interested", "declined", "question"]
    reasoning: str
