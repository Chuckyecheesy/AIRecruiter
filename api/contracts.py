"""Shared contract: enums + Pydantic models used by every lane.

SHARED FILE. Written at kickoff, then frozen. Lane A merges any later changes,
and lane C mirrors them in web/lib/types.ts.

Sections:
  1. Enums (specs 01, 03, 04)                       — lane A
  2. Core records (Decision, Approval, Event, ...)  — lane A
  3. Tool I/O models (spec 02)                      — lane B (TODO at kickoff)
  4. API request/response shapes (spec 06)          — lane C
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


# ───────────────────────────── 1. Enums ─────────────────────────────


class CandidateState(StrEnum):
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


# ─────────────────── Shared helpers (used by routes / dashboard) ───────────────────
# Everything below is additive; the lane A models above are unchanged.

from pydantic import ConfigDict  # noqa: E402
from pydantic.alias_generators import to_camel  # noqa: E402

TERMINAL_STATES = frozenset(
    {CandidateState.SHORTLISTED, CandidateState.REJECTED, CandidateState.DECLINED}
)

RESTRICTED_ACTIONS = (
    ActionName.send_outreach,
    ActionName.send_followup,
    ActionName.schedule_interview,
    ActionName.reject_candidate_proposal,
)

RestrictedAction = Literal[
    "send_outreach", "send_followup", "schedule_interview", "reject_candidate_proposal"
]


class ApprovalStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"
    executed = "executed"


class EventStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"


Disposition = Literal["interested", "not_interested", "asks_question", "no_reply"]


class CoreModel(BaseModel):
    """Accepts snake_case or camelCase input. model_dump() is snake_case (DB);
    API responses serialize camelCase (FastAPI dumps by alias)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PermissionPolicy(CoreModel):
    restricted_actions: list[str] = Field(default_factory=lambda: [a.value for a in RESTRICTED_ACTIONS])
    auto_approve: bool = False
    auto_approve_actions: list[str] = Field(default_factory=list)
    approval_timeout_minutes: Optional[int] = None


class JobForm(CoreModel):
    title: str
    must_haves: list[str] = Field(default_factory=list)
    nice_to_haves: list[str] = Field(default_factory=list)
    location: Optional[str] = None
    seniority: Optional[str] = None
    free_text: Optional[str] = None


class RubricCriterion(CoreModel):
    name: str
    weight: float
    description: Optional[str] = None


class Rubric(CoreModel):
    criteria: list[RubricCriterion] = Field(default_factory=list)
    interview_themes: list[str] = Field(default_factory=list)


# ─────────────────────── API shapes (spec 06, lane C) ───────────────────────
# Responses are camelCase on the wire (mirrors web/lib/types.ts).


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, serialize_by_alias=True)


class CandidateMemoryOut(ApiModel):
    """CandidateMemory as the dashboard sees it (camelCase keys; log entries are free-form)."""

    candidate_id: str
    profile: dict[str, Any] = Field(default_factory=dict)
    research_notes: dict[str, Any] = Field(default_factory=dict)
    contact: dict[str, Any] = Field(default_factory=dict)
    contact_log: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    updated_at: Optional[str] = None


class CandidateOut(ApiModel):
    id: str
    name: str
    state: CandidateState
    score: int | None = None
    headline: str | None = None
    needs_human: bool = False
    trace_url: str | None = None
    pending_approval_id: str | None = None
    demo_disposition: Disposition | None = None
    updated_at: str


class ApprovalOut(ApiModel):
    id: str
    job_id: str
    candidate_id: str
    candidate_name: str
    action: RestrictedAction
    payload: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = ""
    status: ApprovalStatus
    decided_by: Literal["ceo", "auto"] | None = None
    feedback: str | None = None
    created_at: str
    decided_at: str | None = None


class EventOut(ApiModel):
    id: int
    type: EventType
    candidate_id: str | None = None
    candidate_name: str | None = None
    approval_id: str | None = None  # links ApprovalRequested/Granted/Rejected to its card
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class JobSummaryOut(ApiModel):
    id: str
    title: str
    state: JobState
    counts: dict[str, int] = Field(default_factory=dict)  # CandidateState -> count
    pending_approvals: int = 0
    created_at: str
    updated_at: str


class JobDetailOut(JobSummaryOut):
    form: JobForm
    rubric: Rubric | None = None
    shortlist_target: int = 3
    score_threshold: int = 75
    max_candidates: int = 5
    permission_policy: PermissionPolicy = Field(default_factory=PermissionPolicy)


class MessageOut(ApiModel):
    id: str
    channel: Literal["email", "voice"]
    direction: Literal["out", "in"]
    subject: str | None = None
    body: str | None = None
    simulated: bool = True
    created_at: str


class InterviewOut(ApiModel):
    id: str
    conversation_id: str | None = None
    join_url: str | None = None
    expires_at: str | None = None
    questions: list[str] = Field(default_factory=list)
    status: Literal["scheduled", "in_progress", "completed", "expired"]
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    duration_s: int | None = None
    created_at: str
    completed_at: str | None = None


class CriterionScoreOut(ApiModel):
    name: str
    weight: float
    score: float
    evidence: str = ""


class EvaluationOut(ApiModel):
    id: str
    score: int
    criteria: list[CriterionScoreOut] = Field(default_factory=list)
    recommendation: Literal["shortlist", "reject", "borderline"]
    summary: str = ""
    created_at: str


class CandidateDetailOut(CandidateOut):
    job_id: str
    prev_state: CandidateState | None = None
    memory: CandidateMemoryOut | None = None
    messages: list[MessageOut] = Field(default_factory=list)
    interview: InterviewOut | None = None
    evaluation: EvaluationOut | None = None
    events: list[EventOut] = Field(default_factory=list)
    approvals: list[ApprovalOut] = Field(default_factory=list)


class ShortlistEntryOut(ApiModel):
    candidate: CandidateOut
    evaluation: EvaluationOut | None = None


# Requests


class JobCreateIn(ApiModel):
    title: str
    must_haves: list[str] = Field(default_factory=list)
    nice_to_haves: list[str] = Field(default_factory=list)
    location: str | None = None
    seniority: str | None = None
    free_text: str | None = None


class JobPatchIn(ApiModel):
    title: str | None = None
    rubric: Rubric | None = None
    shortlist_target: int | None = None
    score_threshold: int | None = None
    max_candidates: int | None = None
    permission_policy: PermissionPolicy | None = None


class ApproveIn(ApiModel):
    payload: dict[str, Any] | None = None


class RejectIn(ApiModel):
    feedback: str = ""


class CandidatePatchIn(ApiModel):
    demo_disposition: Disposition | None = None


class ChatIn(ApiModel):
    text: str
    approval_id: str | None = None  # focused / replied-to approval card
    candidate_id: str | None = None


class ChatOut(ApiModel):
    reply: str
    approval_id: str | None = None
    action_taken: str | None = None
