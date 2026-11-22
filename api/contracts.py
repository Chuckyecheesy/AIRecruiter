"""Shared enums + Pydantic models (frozen after kickoff; changes go through lane A).

Names mirror web/lib/types.ts exactly. Enum values equal member names so they
store as plain text in SQLite and compare equal to strings.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class CandidateState(str, Enum):
    DISCOVERED = "DISCOVERED"
    RESEARCHED = "RESEARCHED"
    CONTACT_FOUND = "CONTACT_FOUND"
    OUTREACH_DRAFTED = "OUTREACH_DRAFTED"
    APPROVED = "APPROVED"
    CONTACTED = "CONTACTED"
    DECLINED = "DECLINED"
    STALE = "STALE"
    INTERESTED = "INTERESTED"
    INTERVIEWING = "INTERVIEWING"
    INTERVIEWED = "INTERVIEWED"
    EVALUATED = "EVALUATED"
    SHORTLISTED = "SHORTLISTED"
    REJECTED = "REJECTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class JobState(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"


class EventType(str, Enum):
    JobCreated = "JobCreated"
    CandidateFound = "CandidateFound"
    CandidateEnriched = "CandidateEnriched"
    ContactFound = "ContactFound"
    OutreachDrafted = "OutreachDrafted"
    ApprovalRequested = "ApprovalRequested"
    ApprovalGranted = "ApprovalGranted"
    ApprovalRejected = "ApprovalRejected"
    EmailSent = "EmailSent"
    EmailReplied = "EmailReplied"
    InterviewScheduled = "InterviewScheduled"
    InterviewDone = "InterviewDone"
    Evaluated = "Evaluated"
    Tick = "Tick"
    ToolFailed = "ToolFailed"
    Escalated = "Escalated"
    HumanResolved = "HumanResolved"


class ActionName(str, Enum):
    search_candidates = "search_candidates"
    enrich_candidate = "enrich_candidate"
    browser_research = "browser_research"
    find_contact = "find_contact"
    draft_outreach = "draft_outreach"
    redraft_outreach = "redraft_outreach"
    send_outreach = "send_outreach"
    send_followup = "send_followup"
    draft_reply = "draft_reply"
    process_reply = "process_reply"
    mark_stale = "mark_stale"
    schedule_interview = "schedule_interview"
    process_interview = "process_interview"
    evaluate_candidate = "evaluate_candidate"
    shortlist = "shortlist"
    reject_candidate_proposal = "reject_candidate_proposal"
    wait = "wait"
    escalate = "escalate"
    resume = "resume"
    reactivate = "reactivate"


class PermissionClass(str, Enum):
    READ = "READ"
    WRITE = "WRITE"
    RESTRICTED = "RESTRICTED"


class Decision(BaseModel):
    """Structured output of the decide node."""
    action: ActionName
    args: dict[str, Any] = Field(default_factory=dict)
    reasoning: str


class Approval(BaseModel):
    id: str
    job_id: str
    candidate_id: str
    action: ActionName
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "approved", "rejected", "expired", "executed"] = "pending"
    reasoning: str = ""
    decided_by: Optional[Literal["ceo", "auto"]] = None
    feedback: Optional[str] = None
    created_at: Optional[str] = None
    decided_at: Optional[str] = None
    executed_at: Optional[str] = None


class Event(BaseModel):
    id: Optional[int] = None  # autoincrement ordering cursor
    job_id: str
    candidate_id: Optional[str] = None  # None for job-level events
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "processing", "done", "failed"] = "pending"
    not_before: Optional[str] = None
    attempts: int = 0
    created_at: Optional[str] = None
    processed_at: Optional[str] = None


class CandidateMemory(BaseModel):
    """Single source of truth for one candidate (blackboard)."""
    candidate_id: str
    profile: dict[str, Any] = Field(default_factory=dict)
    research_notes: dict[str, Any] = Field(default_factory=dict)
    contact: dict[str, Any] = Field(default_factory=dict)
    contact_log: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    updated_at: Optional[str] = None
