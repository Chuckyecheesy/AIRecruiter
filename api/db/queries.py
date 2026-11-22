"""Read-only queries for the dashboard (lane C).

Never write tables here. Writes go through lane A's api/db/repo.py.
Every function takes an open sqlite3 connection and returns contract models.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from api.contracts import (
    ApprovalOut,
    CandidateDetailOut,
    CandidateMemory,
    CandidateOut,
    CandidateState,
    EvaluationOut,
    EventOut,
    InterviewOut,
    JobDetailOut,
    JobSummaryOut,
    MessageOut,
    ShortlistEntryOut,
)

# Events the dashboard never needs to see (noise).
HIDDEN_EVENT_TYPES = ("Tick",)

APPROVAL_EVENT_TYPES = ("ApprovalRequested", "ApprovalGranted", "ApprovalRejected")


# ───────────────────────────── connection ─────────────────────────────


def db_path() -> Path:
    """Path to the app DB. RECRUITER_DB overrides; else DATA_DIR/recruiter.db (spec 04/07)."""
    if p := os.environ.get("RECRUITER_DB"):
        return Path(p)
    data_dir = os.environ.get("DATA_DIR")
    if not data_dir:
        try:  # lane B's settings, when merged
            from api.config import settings  # type: ignore

            data_dir = str(getattr(settings, "DATA_DIR", None) or getattr(settings, "data_dir", "data"))
        except Exception:
            data_dir = "data"
    return Path(data_dir) / "recruiter.db"


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or db_path()), timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _j(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ───────────────────────────── jobs ─────────────────────────────


def _counts(conn: sqlite3.Connection, job_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM candidates WHERE job_id = ? GROUP BY state", (job_id,)
    ).fetchall()
    return {r["state"]: r["n"] for r in rows}


def _pending_approvals(conn: sqlite3.Connection, job_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM approvals WHERE job_id = ? AND status = 'pending'", (job_id,)
    ).fetchone()[0]


def _job_summary(conn: sqlite3.Connection, r: sqlite3.Row) -> dict[str, Any]:
    return dict(
        id=r["id"],
        title=r["title"],
        state=r["state"],
        counts=_counts(conn, r["id"]),
        pending_approvals=_pending_approvals(conn, r["id"]),
        created_at=r["created_at"],
        updated_at=r["updated_at"],
    )


def list_jobs(conn: sqlite3.Connection) -> list[JobSummaryOut]:
    rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
    return [JobSummaryOut(**_job_summary(conn, r)) for r in rows]


def get_job(conn: sqlite3.Connection, job_id: str) -> JobDetailOut | None:
    r = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not r:
        return None
    form = _j(r["form"], {})
    form.setdefault("title", r["title"])
    rubric = _j(r["rubric"], None)
    policy = _j(r["permission_policy"], {}) or {}
    return JobDetailOut(
        **_job_summary(conn, r),
        form=form,
        rubric=rubric,
        shortlist_target=r["shortlist_target"],
        score_threshold=r["score_threshold"],
        max_candidates=r["max_candidates"],
        permission_policy=policy,
    )


# ───────────────────────────── candidates ─────────────────────────────

_CANDIDATE_SELECT = """
SELECT c.*,
       m.profile AS m_profile,
       (SELECT a.id FROM approvals a
         WHERE a.candidate_id = c.id AND a.status = 'pending'
         ORDER BY a.created_at DESC LIMIT 1) AS pending_approval_id
  FROM candidates c
  LEFT JOIN candidate_memory m ON m.candidate_id = c.id
"""


def _headline(profile: dict[str, Any]) -> str | None:
    if h := profile.get("headline"):
        return h
    raw = profile.get("raw") or {}
    if isinstance(raw, dict) and raw.get("headline"):
        return raw["headline"]
    exp = profile.get("experience") or []
    if exp and isinstance(exp[0], dict):
        title, company = exp[0].get("title"), exp[0].get("company")
        if title and company:
            return f"{title} @ {company}"
        return title or company
    return None


def _candidate(r: sqlite3.Row) -> dict[str, Any]:
    profile = _j(r["m_profile"], {}) or {}
    return dict(
        id=r["id"],
        name=r["name"],
        state=r["state"],
        score=r["score"],
        headline=_headline(profile),
        needs_human=r["state"] == CandidateState.NEEDS_HUMAN,
        trace_url=r["trace_url"],
        pending_approval_id=r["pending_approval_id"],
        demo_disposition=r["demo_disposition"],
        updated_at=r["updated_at"],
    )


def list_candidates(conn: sqlite3.Connection, job_id: str) -> list[CandidateOut]:
    rows = conn.execute(_CANDIDATE_SELECT + " WHERE c.job_id = ? ORDER BY c.created_at", (job_id,)).fetchall()
    return [CandidateOut(**_candidate(r)) for r in rows]


def get_candidate_row(conn: sqlite3.Connection, candidate_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,)).fetchone()


def get_candidate(conn: sqlite3.Connection, candidate_id: str) -> CandidateDetailOut | None:
    r = conn.execute(_CANDIDATE_SELECT + " WHERE c.id = ?", (candidate_id,)).fetchone()
    if not r:
        return None

    mem_row = conn.execute("SELECT * FROM candidate_memory WHERE candidate_id = ?", (candidate_id,)).fetchone()
    memory = None
    if mem_row:
        memory = CandidateMemory(
            candidate_id=candidate_id,
            profile=_j(mem_row["profile"], {}),
            research_notes=_j(mem_row["research_notes"], {}),
            contact=_j(mem_row["contact"], {}),
            contact_log=_j(mem_row["contact_log"], []),
            decisions=_j(mem_row["decisions"], []),
            summary=mem_row["summary"] or "",
            updated_at=mem_row["updated_at"],
        )

    messages = [
        MessageOut(
            id=m["id"],
            channel=m["channel"],
            direction=m["direction"],
            subject=m["subject"],
            body=m["body"],
            simulated=bool(m["simulated"]),
            created_at=m["created_at"],
        )
        for m in conn.execute(
            "SELECT * FROM messages WHERE candidate_id = ? ORDER BY created_at", (candidate_id,)
        ).fetchall()
    ]

    iv = conn.execute(
        "SELECT * FROM interviews WHERE candidate_id = ? ORDER BY created_at DESC LIMIT 1", (candidate_id,)
    ).fetchone()
    interview = _interview(iv) if iv else None

    ev = conn.execute(
        "SELECT * FROM evaluations WHERE candidate_id = ? ORDER BY created_at DESC LIMIT 1", (candidate_id,)
    ).fetchone()
    evaluation = _evaluation(ev) if ev else None

    approvals = [
        _approval(a)
        for a in conn.execute(
            _APPROVAL_SELECT + " WHERE a.candidate_id = ? ORDER BY a.created_at", (candidate_id,)
        ).fetchall()
    ]

    events = _events(conn, "e.candidate_id = ?", (candidate_id,), limit=500)

    return CandidateDetailOut(
        **_candidate(r),
        job_id=r["job_id"],
        prev_state=r["prev_state"],
        memory=memory,
        messages=messages,
        interview=interview,
        evaluation=evaluation,
        events=events,
        approvals=approvals,
    )


def _interview(r: sqlite3.Row) -> InterviewOut:
    return InterviewOut(
        id=r["id"],
        conversation_id=r["conversation_id"],
        join_url=r["join_url"],
        expires_at=r["expires_at"],
        questions=_j(r["questions"], []),
        status=r["status"],
        transcript=_j(r["transcript"], []),
        duration_s=r["duration_s"],
        created_at=r["created_at"],
        completed_at=r["completed_at"],
    )


def _evaluation(r: sqlite3.Row) -> EvaluationOut:
    return EvaluationOut(
        id=r["id"],
        score=r["score"],
        criteria=_j(r["criteria"], []),
        recommendation=r["recommendation"],
        summary=r["summary"] or "",
        created_at=r["created_at"],
    )


def shortlist(conn: sqlite3.Connection, job_id: str) -> list[ShortlistEntryOut]:
    """Shortlisted first, then evaluated candidates, each ranked by score."""
    rows = conn.execute(
        _CANDIDATE_SELECT
        + """ WHERE c.job_id = ? AND c.state IN ('SHORTLISTED', 'EVALUATED')
              ORDER BY (c.state = 'SHORTLISTED') DESC, c.score DESC""",
        (job_id,),
    ).fetchall()
    out = []
    for r in rows:
        ev = conn.execute(
            "SELECT * FROM evaluations WHERE candidate_id = ? ORDER BY created_at DESC LIMIT 1", (r["id"],)
        ).fetchone()
        out.append(ShortlistEntryOut(candidate=CandidateOut(**_candidate(r)), evaluation=_evaluation(ev) if ev else None))
    return out


# ───────────────────────────── approvals ─────────────────────────────

_APPROVAL_SELECT = """
SELECT a.*, c.name AS candidate_name
  FROM approvals a
  LEFT JOIN candidates c ON c.id = a.candidate_id
"""


def _approval(r: sqlite3.Row) -> ApprovalOut:
    return ApprovalOut(
        id=r["id"],
        job_id=r["job_id"],
        candidate_id=r["candidate_id"],
        candidate_name=r["candidate_name"] or "",
        action=r["action"],
        payload=_j(r["payload"], {}),
        reasoning=r["reasoning"] or "",
        status=r["status"],
        decided_by=r["decided_by"],
        feedback=r["feedback"],
        created_at=r["created_at"],
        decided_at=r["decided_at"],
    )


def list_approvals(conn: sqlite3.Connection, job_id: str, status: str | None = None) -> list[ApprovalOut]:
    if status:
        rows = conn.execute(
            _APPROVAL_SELECT + " WHERE a.job_id = ? AND a.status = ? ORDER BY a.created_at", (job_id, status)
        ).fetchall()
    else:
        rows = conn.execute(_APPROVAL_SELECT + " WHERE a.job_id = ? ORDER BY a.created_at", (job_id,)).fetchall()
    return [_approval(r) for r in rows]


def get_approval(conn: sqlite3.Connection, approval_id: str) -> ApprovalOut | None:
    r = conn.execute(_APPROVAL_SELECT + " WHERE a.id = ?", (approval_id,)).fetchone()
    return _approval(r) if r else None


def latest_pending_approval(
    conn: sqlite3.Connection, job_id: str, candidate_id: str | None = None
) -> ApprovalOut | None:
    sql = _APPROVAL_SELECT + " WHERE a.job_id = ? AND a.status = 'pending'"
    args: list[Any] = [job_id]
    if candidate_id:
        sql += " AND a.candidate_id = ?"
        args.append(candidate_id)
    r = conn.execute(sql + " ORDER BY a.created_at DESC LIMIT 1", args).fetchone()
    return _approval(r) if r else None


# ───────────────────────────── events ─────────────────────────────

# approval_id: prefer payload.approval_id; for approval events without it, fall back to the
# candidate's most recent approval created at or before the event.
_EVENT_SELECT = f"""
SELECT e.*, c.name AS candidate_name,
       COALESCE(
         json_extract(e.payload, '$.approval_id'),
         CASE WHEN e.type IN ({",".join(f"'{t}'" for t in APPROVAL_EVENT_TYPES)}) THEN
           (SELECT a.id FROM approvals a
             WHERE a.candidate_id = e.candidate_id AND julianday(a.created_at) <= julianday(e.created_at)
             ORDER BY a.created_at DESC LIMIT 1)
         END
       ) AS approval_id
  FROM events e
  LEFT JOIN candidates c ON c.id = e.candidate_id
"""


def _event(r: sqlite3.Row) -> EventOut:
    return EventOut(
        id=r["id"],
        type=r["type"],
        candidate_id=r["candidate_id"],
        candidate_name=r["candidate_name"],
        approval_id=r["approval_id"],
        payload=_j(r["payload"], {}),
        # A delayed event (persona reply) "happens" at not_before, not when the row was written.
        created_at=r["not_before"] or r["created_at"],
    )


def _events(conn: sqlite3.Connection, where: str, args: tuple, limit: int) -> list[EventOut]:
    hidden = ",".join(f"'{t}'" for t in HIDDEN_EVENT_TYPES)
    now = iso(_now())
    rows = conn.execute(
        _EVENT_SELECT
        + f" WHERE ({where}) AND e.type NOT IN ({hidden})"
        + " AND (e.not_before IS NULL OR julianday(e.not_before) <= julianday(?))"
        + " ORDER BY e.id LIMIT ?",
        (*args, now, limit),
    ).fetchall()
    return [_event(r) for r in rows]


def events_after(
    conn: sqlite3.Connection,
    job_id: str,
    after: int = 0,
    *,
    due_since: datetime | None = None,
    limit: int = 500,
) -> list[EventOut]:
    """Events for a job with id > after that are already due.

    Delayed events (not_before in the future, e.g. persona replies) are withheld until due.
    Because they can have an id *below* the cursor, `due_since` also returns older-id events
    whose not_before fell in (due_since, now]. Clients dedupe by id.
    """
    if due_since is None:
        return _events(conn, "e.job_id = ? AND e.id > ?", (job_id, after), limit)
    return _events(
        conn,
        "e.job_id = ? AND (e.id > ? OR (e.not_before IS NOT NULL AND julianday(e.not_before) > julianday(?)))",
        (job_id, after, iso(due_since)),
        limit,
    )


def default_due_window() -> datetime:
    """For stateless polling: re-check delayed events that became due recently."""
    return _now() - timedelta(seconds=30)
