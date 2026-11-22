"""Candidate routes (spec 06)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from api.contracts import (
    TERMINAL_STATES,
    CandidateDetailOut,
    CandidateOut,
    CandidatePatchIn,
    CandidateState,
)
from api.db import queries
from api.routes.deps import call_repo, get_conn

router = APIRouter(tags=["candidates"])


def _candidate_or_404(conn: sqlite3.Connection, candidate_id: str) -> CandidateDetailOut:
    c = queries.get_candidate(conn, candidate_id)
    if not c:
        raise HTTPException(404, "candidate not found")
    return c


@router.get("/jobs/{job_id}/candidates", response_model=list[CandidateOut])
def list_candidates(job_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    if not queries.get_job(conn, job_id):
        raise HTTPException(404, "job not found")
    return queries.list_candidates(conn, job_id)


@router.get("/candidates/{candidate_id}", response_model=CandidateDetailOut)
def get_candidate(candidate_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return _candidate_or_404(conn, candidate_id)


@router.post("/candidates/{candidate_id}/resume", response_model=CandidateDetailOut)
def resume_candidate(candidate_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    """NEEDS_HUMAN -> prev_state (emits HumanResolved), or STALE -> reactivate."""
    c = _candidate_or_404(conn, candidate_id)
    if c.state not in (CandidateState.NEEDS_HUMAN, CandidateState.STALE):
        raise HTTPException(409, f"candidate is {c.state.value}, expected NEEDS_HUMAN or STALE")
    call_repo("resume_candidate", candidate_id)
    return _candidate_or_404(conn, candidate_id)


@router.post("/candidates/{candidate_id}/skip", response_model=CandidateDetailOut)
def skip_candidate(candidate_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    c = _candidate_or_404(conn, candidate_id)
    if c.state in TERMINAL_STATES:
        raise HTTPException(409, f"candidate is already {c.state.value}")
    call_repo("skip_candidate", candidate_id)
    return _candidate_or_404(conn, candidate_id)


@router.patch("/candidates/{candidate_id}", response_model=CandidateDetailOut)
def patch_candidate(candidate_id: str, body: CandidatePatchIn, conn: sqlite3.Connection = Depends(get_conn)):
    _candidate_or_404(conn, candidate_id)
    if "demo_disposition" in body.model_fields_set:
        call_repo("set_demo_disposition", candidate_id, body.demo_disposition)
    return _candidate_or_404(conn, candidate_id)
