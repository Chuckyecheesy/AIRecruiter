"""DEV/TEST ONLY: a minimal stand-in for lane A's api/db/repo.py write functions.

Implements exactly the interface documented in api/routes/deps.py, so lane C's routes
and UI can be exercised before lane A merges. It does NOT run the agent.

Enable for a local server with REPO_IMPL=fake. Delete once api/db/repo.py is real.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from api.db.queries import connect
from scripts.reset_demo import ulid


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _conn():
    return connect()


def emit_event(job_id: str, candidate_id: str | None, type: str, payload: dict | None = None,
               not_before: str | None = None) -> int:
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO events (job_id, candidate_id, type, payload, status, not_before, attempts, created_at)"
            " VALUES (?, ?, ?, ?, 'pending', ?, 0, ?)",
            (job_id, candidate_id, str(type), json.dumps(payload or {}), not_before, _now()),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]
    finally:
        conn.close()


def create_job(form: dict, rubric: dict | None) -> str:
    job_id = ulid()
    now = _now()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO jobs (id, title, form, rubric, state, permission_policy, agent_thread_id, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, 'DRAFT', ?, ?, ?, ?)",
            (job_id, form["title"], json.dumps(form), json.dumps(rubric) if rubric else None,
             json.dumps({"restricted_actions": ["send_outreach", "send_followup", "schedule_interview",
                                                "reject_candidate_proposal"], "auto_approve": False,
                         "auto_approve_actions": [], "approval_timeout_minutes": None}),
             f"{job_id}:job", now, now),
        )
        conn.commit()
        return job_id
    finally:
        conn.close()


def update_job(job_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols, vals = [], []
    for k, v in fields.items():
        cols.append(f"{k} = ?")
        vals.append(json.dumps(v) if isinstance(v, (dict, list)) else v)
    conn = _conn()
    try:
        conn.execute(f"UPDATE jobs SET {', '.join(cols)}, updated_at = ? WHERE id = ?", (*vals, _now(), job_id))
        conn.commit()
    finally:
        conn.close()


def set_job_state(job_id: str, state: str) -> None:
    update_job(job_id, state=str(state))


def decide_approval(approval_id: str, approved: bool, *, payload: dict | None = None,
                    feedback: str | None = None, decided_by: str = "ceo") -> None:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if not row or row["status"] != "pending":
            raise ValueError("approval is not pending")
        status = "approved" if approved else "rejected"
        new_payload = json.dumps(payload) if payload is not None else row["payload"]
        conn.execute(
            "UPDATE approvals SET status = ?, payload = ?, feedback = ?, decided_by = ?, decided_at = ? WHERE id = ?",
            (status, new_payload, feedback, decided_by, _now(), approval_id),
        )
        conn.commit()
        job_id, cand = row["job_id"], row["candidate_id"]
    finally:
        conn.close()
    ev = "ApprovalGranted" if approved else "ApprovalRejected"
    emit_event(job_id, cand, ev, {"approval_id": approval_id, "action": row["action"], "feedback": feedback,
                                  "decided_by": decided_by})


def resume_candidate(candidate_id: str) -> None:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
        target = row["prev_state"] if row["state"] == "NEEDS_HUMAN" else "CONTACTED"
        conn.execute("UPDATE candidates SET state = ?, prev_state = NULL, updated_at = ? WHERE id = ?",
                     (target, _now(), candidate_id))
        conn.commit()
        job_id = row["job_id"]
    finally:
        conn.close()
    emit_event(job_id, candidate_id, "HumanResolved", {"resolution": "resume", "state": target})


def skip_candidate(candidate_id: str) -> None:
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()
        conn.execute("UPDATE candidates SET state = 'REJECTED', updated_at = ? WHERE id = ?", (_now(), candidate_id))
        conn.commit()
        job_id = row["job_id"]
    finally:
        conn.close()
    emit_event(job_id, candidate_id, "HumanResolved", {"resolution": "skip", "state": "REJECTED"})


def set_demo_disposition(candidate_id: str, disposition: str | None) -> None:
    conn = _conn()
    try:
        conn.execute("UPDATE candidates SET demo_disposition = ?, updated_at = ? WHERE id = ?",
                     (disposition, _now(), candidate_id))
        conn.commit()
    finally:
        conn.close()
