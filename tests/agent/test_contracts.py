"""Red tests for api/contracts.py (lane A kickoff).

Source of truth: specs/01-state-machine.md, 03-permissions.md, 04-data-model.md.
Names must match exactly; they are mirrored in web/lib/types.ts.
"""
import pytest
from pydantic import ValidationError

from api.contracts import (
    Approval,
    ActionName,
    CandidateMemory,
    CandidateState,
    Decision,
    Event,
    EventType,
    JobState,
    PermissionClass,
)

CANDIDATE_STATES = {
    "DISCOVERED", "RESEARCHED", "CONTACT_FOUND", "OUTREACH_DRAFTED", "APPROVED",
    "CONTACTED", "DECLINED", "STALE", "INTERESTED", "INTERVIEWING",
    "INTERVIEWED", "EVALUATED", "SHORTLISTED", "REJECTED", "NEEDS_HUMAN",
}
JOB_STATES = {"DRAFT", "ACTIVE", "PAUSED", "COMPLETE"}
EVENT_TYPES = {
    "JobCreated", "CandidateFound", "CandidateEnriched", "ContactFound",
    "OutreachDrafted", "ApprovalRequested", "ApprovalGranted", "ApprovalRejected",
    "EmailSent", "EmailReplied", "InterviewScheduled", "InterviewDone",
    "Evaluated", "Tick", "ToolFailed", "Escalated", "HumanResolved",
}
ACTIONS = {
    "search_candidates", "enrich_candidate", "browser_research", "find_contact",
    "draft_outreach", "redraft_outreach", "send_outreach", "send_followup",
    "draft_reply", "process_reply", "mark_stale", "schedule_interview",
    "process_interview", "evaluate_candidate", "shortlist",
    "reject_candidate_proposal", "wait", "escalate", "resume", "reactivate",
}


@pytest.mark.parametrize(
    "enum_cls, expected",
    [
        (CandidateState, CANDIDATE_STATES),
        (JobState, JOB_STATES),
        (EventType, EVENT_TYPES),
        (ActionName, ACTIONS),
        (PermissionClass, {"READ", "WRITE", "RESTRICTED"}),
    ],
)
def test_enum_members_match_spec(enum_cls, expected):
    assert {m.name for m in enum_cls} == expected


@pytest.mark.parametrize("enum_cls", [CandidateState, JobState, EventType, ActionName, PermissionClass])
def test_enum_values_equal_names_and_are_strings(enum_cls):
    # Stored as text in SQLite and mirrored in TS, so value == name, and str-comparable.
    for m in enum_cls:
        assert m.value == m.name
        assert m == m.name


def test_there_are_exactly_17_event_types():
    assert len(EventType) == 17


def test_decision_requires_action_and_reasoning_and_defaults_args():
    d = Decision(action=ActionName.enrich_candidate, reasoning="new candidate")
    assert d.args == {}
    with pytest.raises(ValidationError):
        Decision(reasoning="no action")
    with pytest.raises(ValidationError):
        Decision(action="not_an_action", reasoning="x")


def test_approval_defaults_and_status_validation():
    a = Approval(
        id="01A", job_id="J", candidate_id="C",
        action=ActionName.send_outreach, payload={"subject": "s", "body": "b"},
        reasoning="draft ready",
    )
    assert a.status == "pending"
    assert a.decided_by is None and a.feedback is None
    assert a.decided_at is None and a.executed_at is None
    for ok in ("pending", "approved", "rejected", "expired", "executed"):
        assert Approval(**{**a.model_dump(), "status": ok}).status == ok
    with pytest.raises(ValidationError):
        Approval(**{**a.model_dump(), "status": "maybe"})
    with pytest.raises(ValidationError):
        Approval(**{**a.model_dump(), "decided_by": "robot"})


def test_event_defaults():
    e = Event(job_id="J", type=EventType.JobCreated)
    assert e.candidate_id is None  # job-level event
    assert e.payload == {}
    assert e.status == "pending"
    assert e.not_before is None
    assert e.attempts == 0
    with pytest.raises(ValidationError):
        Event(job_id="J", type="Bogus")
    with pytest.raises(ValidationError):
        Event(job_id="J", type=EventType.Tick, status="weird")


def test_candidate_memory_defaults_are_independent_per_instance():
    m = CandidateMemory(candidate_id="C")
    assert m.profile == {} and m.research_notes == {} and m.contact == {}
    assert m.contact_log == [] and m.decisions == []
    assert m.summary == ""
    m.contact_log.append({"ts": "t", "channel": "email", "action": "send_outreach", "message_id": "m"})
    assert CandidateMemory(candidate_id="D").contact_log == []


def test_candidate_memory_roundtrips_through_json():
    m = CandidateMemory(candidate_id="C", summary="s", decisions=[{"ts": "t", "action": "wait", "reasoning": "r", "outcome": "ok"}])
    assert CandidateMemory.model_validate_json(m.model_dump_json()) == m
