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
from typing import Any, Literal, Optional

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
    INTERESTED = "INTERESTED"
    DECLINED = "DECLINED"
    STALE = "STALE"
    INTERVIEWING = "INTERVIEWING"
    INTERVIEWED = "INTERVIEWED"
    EVALUATED = "EVALUATED"
    SHORTLISTED = "SHORTLISTED"
    REJECTED = "REJECTED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


TERMINAL_STATES = frozenset(
    {CandidateState.SHORTLISTED, CandidateState.REJECTED, CandidateState.DECLINED}
)


class JobState(StrEnum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETE = "COMPLETE"


class EventType(StrEnum):
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


class ActionName(StrEnum):
    search_candidates = "search_candidates"
    enrich_candidate = "enrich_candidate"
    browser_research = "browser_research"
    find_contact = "find_contact"
    draft_outreach = "draft_outreach"
    redraft_outreach = "redraft_outreach"
    draft_reply = "draft_reply"
    send_outreach = "send_outreach"
    send_followup = "send_followup"
    process_reply = "process_reply"
    wait = "wait"
    mark_stale = "mark_stale"
    schedule_interview = "schedule_interview"
    process_interview = "process_interview"
    evaluate_candidate = "evaluate_candidate"
    shortlist = "shortlist"
    reject_candidate_proposal = "reject_candidate_proposal"
    escalate = "escalate"
    resume = "resume"
    reactivate = "reactivate"


class PermissionClass(StrEnum):
    READ = "READ"
    WRITE = "WRITE"
    RESTRICTED = "RESTRICTED"


RESTRICTED_ACTIONS = (
    ActionName.send_outreach,
    ActionName.send_followup,
    ActionName.schedule_interview,
    ActionName.reject_candidate_proposal,
)

RestrictedAction = Literal[
    "send_outreach", "send_followup", "schedule_interview", "reject_candidate_proposal"
]


class ApprovalStatus(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"
    executed = "executed"


class EventStatus(StrEnum):
    pending = "pending"
    processing = "processing"
    done = "done"
    failed = "failed"


Disposition = Literal["interested", "not_interested", "asks_question", "no_reply"]


# ─────────────────────────── 2. Core records ───────────────────────────


class CoreModel(BaseModel):
    """Accepts snake_case or camelCase input. model_dump() is snake_case (DB);
    API responses serialize camelCase (FastAPI dumps by alias)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PermissionPolicy(CoreModel):
    restricted_actions: list[str] = Field(default_factory=lambda: [a.value for a in RESTRICTED_ACTIONS])
    auto_approve: bool = False
    auto_approve_actions: list[str] = Field(default_factory=list)
    approval_timeout_minutes: int | None = None


class JobForm(CoreModel):
    title: str
    must_haves: list[str] = Field(default_factory=list)
    nice_to_haves: list[str] = Field(default_factory=list)
    location: str | None = None
    seniority: str | None = None
    free_text: str | None = None


class RubricCriterion(CoreModel):
    name: str
    weight: float
    description: str | None = None


class Rubric(CoreModel):
    criteria: list[RubricCriterion] = Field(default_factory=list)
    interview_themes: list[str] = Field(default_factory=list)


class Decision(CoreModel):
    action: ActionName
    args: dict[str, Any] = Field(default_factory=dict)
    reasoning: str = ""


# Documentation of the common keys only. CandidateMemory stores these lists as plain dicts because
# lane A writes several entry kinds with different keys (e.g. contact_log entries of kind
# email_out / email_in / draft / interview / reactivated, with to/subject/body/turns) and reads
# them with dict access. Extra keys are allowed if you do validate an entry with these models.
class DecisionLogEntry(CoreModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    ts: str | None = None
    action: str | None = None
    reasoning: str = ""
    outcome: str | None = None


class ContactLogEntry(CoreModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    ts: str | None = None
    kind: str | None = None  # lane A: email_out | email_in | draft | interview | reactivated
    channel: str | None = None
    action: str | None = None
    message_id: str | None = None


class CandidateMemory(CoreModel):
    candidate_id: str
    profile: dict[str, Any] = Field(default_factory=dict)
    research_notes: dict[str, Any] = Field(default_factory=dict)
    contact: dict[str, Any] = Field(default_factory=dict)
    contact_log: list[dict[str, Any]] = Field(default_factory=list)  # see ContactLogEntry
    decisions: list[dict[str, Any]] = Field(default_factory=list)    # see DecisionLogEntry
    summary: str = ""
    updated_at: str | None = None


class ApprovalRecord(CoreModel):
    """DB-shaped approval row (snake_case)."""

    id: str
    job_id: str
    candidate_id: str
    action: ActionName  # always one of RESTRICTED_ACTIONS in practice (lane A enforces it)
    payload: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.pending
    reasoning: str = ""
    decided_by: Literal["ceo", "auto"] | None = None
    feedback: str | None = None
    created_at: str | None = None  # set by the DB layer on insert
    decided_at: str | None = None
    executed_at: str | None = None


# Lane A's original name (api/db/repo.py, api/agent/*). Same model.
Approval = ApprovalRecord


class Event(CoreModel):
    """DB-shaped events row (snake_case)."""

    id: int | None = None  # autoincrement cursor; None before insert
    job_id: str
    candidate_id: str | None = None
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    status: EventStatus = EventStatus.pending
    not_before: str | None = None
    attempts: int = 0
    created_at: str | None = None  # set by the DB layer on insert
    processed_at: str | None = None


# ───────────────────────── 3. Tool I/O (lane B) ─────────────────────────
# TODO(lane B, kickoff): SearchCriteria, PloidCandidate, EnrichedProfile, ContactInfo,
# BrowserFindings, SendReceipt, InterviewSession, InterviewTranscript, Draft, Evaluation.


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
