"""Jobs routes (spec 06). Writes go through lane A's repo via deps.call_repo."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from api.contracts import (
    EventType,
    JobCreateIn,
    JobDetailOut,
    JobForm,
    JobPatchIn,
    JobState,
    JobSummaryOut,
    ShortlistEntryOut,
)
from api.db import queries
from api.routes.deps import call_repo, generate_rubric, get_conn, get_repo

router = APIRouter(tags=["jobs"])


def _job_or_404(conn: sqlite3.Connection, job_id: str) -> JobDetailOut:
    job = queries.get_job(conn, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@router.get("/jobs", response_model=list[JobSummaryOut])
def list_jobs(conn: sqlite3.Connection = Depends(get_conn)):
    return queries.list_jobs(conn)


@router.post("/jobs", response_model=JobDetailOut, status_code=201)
async def create_job(body: JobCreateIn, conn: sqlite3.Connection = Depends(get_conn)):
    form = JobForm(**body.model_dump(by_alias=False))
    rubric = await generate_rubric(form)
    job_id = call_repo("create_job", form.title, form.model_dump(), rubric=rubric.model_dump())
    return _job_or_404(conn, job_id)


@router.get("/jobs/{job_id}", response_model=JobDetailOut)
def get_job(job_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return _job_or_404(conn, job_id)


@router.patch("/jobs/{job_id}", response_model=JobDetailOut)
def patch_job(job_id: str, body: JobPatchIn, conn: sqlite3.Connection = Depends(get_conn)):
    _job_or_404(conn, job_id)
    fields = body.model_dump(by_alias=False, exclude_unset=True)
    if fields:
        call_repo("update_job", job_id, **fields)
    return _job_or_404(conn, job_id)


@router.post("/jobs/{job_id}/start", response_model=JobDetailOut)
def start_job(job_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    job = _job_or_404(conn, job_id)
    if job.state != JobState.DRAFT:
        raise HTTPException(409, f"job is {job.state}, expected DRAFT")
    repo = get_repo()
    if hasattr(repo, "start_job"):  # if lane A bundles state change + JobCreated
        repo.start_job(job_id)
    else:
        call_repo("set_job_state", job_id, JobState.ACTIVE)
        call_repo("emit_event", job_id=job_id, type=EventType.JobCreated, candidate_id=None, payload={})
    return _job_or_404(conn, job_id)


@router.post("/jobs/{job_id}/pause", response_model=JobDetailOut)
def pause_job(job_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    job = _job_or_404(conn, job_id)
    if job.state != JobState.ACTIVE:
        raise HTTPException(409, f"job is {job.state}, expected ACTIVE")
    call_repo("set_job_state", job_id, JobState.PAUSED)
    return _job_or_404(conn, job_id)


@router.post("/jobs/{job_id}/resume", response_model=JobDetailOut)
def resume_job(job_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    job = _job_or_404(conn, job_id)
    if job.state != JobState.PAUSED:
        raise HTTPException(409, f"job is {job.state}, expected PAUSED")
    call_repo("set_job_state", job_id, JobState.ACTIVE)
    return _job_or_404(conn, job_id)


@router.get("/jobs/{job_id}/shortlist", response_model=list[ShortlistEntryOut])
def get_shortlist(job_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    _job_or_404(conn, job_id)
    return queries.shortlist(conn, job_id)
