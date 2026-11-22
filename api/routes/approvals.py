"""Approval routes (spec 03/06). Decisions go through repo.decide_approval (lane A),
which flips the row and emits ApprovalGranted / ApprovalRejected."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query

from api.contracts import ApprovalOut, ApprovalStatus, ApproveIn, RejectIn
from api.db import queries
from api.routes.deps import call_repo, get_conn

router = APIRouter(tags=["approvals"])


def _pending_or_409(conn: sqlite3.Connection, approval_id: str) -> ApprovalOut:
    a = queries.get_approval(conn, approval_id)
    if not a:
        raise HTTPException(404, "approval not found")
    if a.status != ApprovalStatus.pending:
        raise HTTPException(409, f"approval is already {a.status.value}")
    return a


@router.get("/jobs/{job_id}/approvals", response_model=list[ApprovalOut])
def list_approvals(
    job_id: str,
    status: ApprovalStatus | None = Query(None),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return queries.list_approvals(conn, job_id, status.value if status else None)


@router.get("/approvals/{approval_id}", response_model=ApprovalOut)
def get_approval(approval_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    a = queries.get_approval(conn, approval_id)
    if not a:
        raise HTTPException(404, "approval not found")
    return a


def approve(conn: sqlite3.Connection, approval_id: str, payload: dict | None) -> ApprovalOut:
    _pending_or_409(conn, approval_id)
    call_repo("decide_approval", approval_id, True, payload=payload)
    return queries.get_approval(conn, approval_id)  # type: ignore[return-value]


def reject(conn: sqlite3.Connection, approval_id: str, feedback: str) -> ApprovalOut:
    _pending_or_409(conn, approval_id)
    call_repo("decide_approval", approval_id, False, feedback=feedback)
    return queries.get_approval(conn, approval_id)  # type: ignore[return-value]


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalOut)
def approve_route(approval_id: str, body: ApproveIn | None = None, conn: sqlite3.Connection = Depends(get_conn)):
    return approve(conn, approval_id, body.payload if body else None)


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalOut)
def reject_route(approval_id: str, body: RejectIn, conn: sqlite3.Connection = Depends(get_conn)):
    return reject(conn, approval_id, body.feedback)
