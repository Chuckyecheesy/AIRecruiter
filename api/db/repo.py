"""Write functions + agent reads (lane A). SIGNATURES ONLY for now (kickoff stubs).

Lane C imports these; bodies land in the 0:20-1:10 phase. All functions are
sync (sqlite3). Every connection must run `PRAGMA foreign_keys=ON`.
"""
from __future__ import annotations

from typing import Any, Optional

from api.contracts import EventType, JobState


def create_job(
    title: str,
    form: dict[str, Any],
    *,
    rubric: Optional[dict[str, Any]] = None,
    permission_policy: Optional[dict[str, Any]] = None,
    shortlist_target: int = 3,
    score_threshold: int = 75,
    max_candidates: int = 5,
) -> str:
    """Insert a DRAFT job; return its ULID."""
    ...


def update_job(job_id: str, **fields: Any) -> None:
    """Edit rubric, thresholds, permission policy (incl. auto_approve), etc."""
    ...


def set_job_state(job_id: str, state: JobState) -> None:
    ...


def emit_event(
    job_id: str,
    type: EventType,
    candidate_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    not_before: Optional[str] = None,
) -> int:
    """Insert a pending event row; return its autoincrement id."""
    ...


def decide_approval(
    approval_id: str,
    approved: bool,
    payload: Optional[dict[str, Any]] = None,
    feedback: Optional[str] = None,
) -> None:
    """CEO decision. Approve (optionally with edited payload) -> ApprovalGranted;
    reject (with feedback) -> ApprovalRejected."""
    ...


def resume_candidate(candidate_id: str) -> None:
    """NEEDS_HUMAN -> prev_state (or STALE -> CONTACTED); emits HumanResolved."""
    ...


def skip_candidate(candidate_id: str) -> None:
    """CEO override: move to a terminal state without agent action (logged)."""
    ...


def set_demo_disposition(candidate_id: str, disposition: Optional[str]) -> None:
    """Pin the persona reply: interested | not_interested | asks_question | no_reply."""
    ...
