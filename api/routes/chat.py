"""Composer -> POST /jobs/{id}/chat (lane C).

Must-have: text sent while an approval is focused/replied-to becomes the rejection
feedback for it (the redraft demo moment).

It never runs agent actions itself. It only uses the same approval and job-state
functions the buttons use.

Lightweight command words (until the Claude-with-tools version lands):
    approve            -> approve the targeted approval
    pause / resume     -> job state
    status             -> pipeline summary
    anything else      -> rejection feedback for the targeted pending approval
"""

from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from api.contracts import ApprovalStatus, CandidateState, ChatIn, ChatOut, JobState
from api.db import queries
from api.routes import approvals as approval_routes
from api.routes.deps import call_repo, get_conn

router = APIRouter(tags=["chat"])

_APPROVE_RE = re.compile(r"^\s*(approve|approved|lgtm|ship it|send it)[\s.!]*$", re.I)
_PAUSE_RE = re.compile(r"^\s*pause( the)?( agent| recruiter)?[\s.!]*$", re.I)
_RESUME_RE = re.compile(r"^\s*(resume|continue|unpause)( the)?( agent| recruiter)?[\s.!]*$", re.I)
_STATUS_RE = re.compile(r"^\s*(status|who'?s shortlisted\??|where are we\??)[\s.!?]*$", re.I)


def _target_approval(conn: sqlite3.Connection, job_id: str, body: ChatIn):
    if body.approval_id:
        a = queries.get_approval(conn, body.approval_id)
        if not a or a.job_id != job_id:
            raise HTTPException(404, "approval not found for this job")
        return a
    return queries.latest_pending_approval(conn, job_id, body.candidate_id)


def _status_text(conn: sqlite3.Connection, job_id: str) -> str:
    cands = queries.list_candidates(conn, job_id)
    if not cands:
        return "No candidates yet."
    by_state: dict[str, list[str]] = {}
    for c in cands:
        by_state.setdefault(c.state, []).append(c.name + (f" ({c.score})" if c.score is not None else ""))
    lines = [f"{state}: {', '.join(names)}" for state, names in by_state.items()]
    pending = queries.list_approvals(conn, job_id, "pending")
    if pending:
        lines.append(f"{len(pending)} approval(s) waiting on you.")
    shortlisted = by_state.get(CandidateState.SHORTLISTED)
    head = f"Shortlisted: {', '.join(shortlisted)}." if shortlisted else "Nobody shortlisted yet."
    return head + "\n" + "\n".join(lines)


@router.post("/jobs/{job_id}/chat", response_model=ChatOut)
def chat(job_id: str, body: ChatIn, conn: sqlite3.Connection = Depends(get_conn)):
    job = queries.get_job(conn, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "empty message")

    if _STATUS_RE.match(text):
        return ChatOut(reply=_status_text(conn, job_id), action_taken="status")

    if _PAUSE_RE.match(text):
        if job.state != JobState.ACTIVE:
            return ChatOut(reply=f"The recruiter is {job.state.lower()}, nothing to pause.")
        call_repo("set_job_state", job_id, JobState.PAUSED)
        return ChatOut(reply="Paused. No new decisions until you resume.", action_taken="pause")

    if _RESUME_RE.match(text):
        if job.state != JobState.PAUSED:
            return ChatOut(reply=f"The recruiter is {job.state.lower()}, nothing to resume.")
        call_repo("set_job_state", job_id, JobState.ACTIVE)
        return ChatOut(reply="Resumed.", action_taken="resume")

    target = _target_approval(conn, job_id, body)
    if target is None or target.status != ApprovalStatus.pending:
        return ChatOut(
            reply="There's no pending approval to apply that to. Select a card first, "
            "or ask for `status`.",
        )

    if _APPROVE_RE.match(text):
        approval_routes.approve(conn, target.id, None)
        return ChatOut(
            reply=f"Approved {target.action} for {target.candidate_name}.",
            approval_id=target.id,
            action_taken="approve",
        )

    approval_routes.reject(conn, target.id, text)
    return ChatOut(
        reply=f"Sent back {target.action} for {target.candidate_name} with your feedback. The agent will revise it.",
        approval_id=target.id,
        action_taken="reject",
    )
