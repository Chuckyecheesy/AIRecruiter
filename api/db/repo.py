"""Write functions + agent reads (lane A).

Lane C imports the kickoff signatures; all functions are sync (sqlite3). Every
connection runs `PRAGMA foreign_keys=ON` and WAL. Each `get_conn()` block is one
`BEGIN IMMEDIATE` transaction, so multi-row operations (approve + state move +
event) are atomic and read-then-write races (locks, approval execution) are
serialized by SQLite's write lock.

Errors: unknown ids -> LookupError; illegal values / state conflicts -> ValueError
(pydantic ValidationError is a ValueError); FK violations -> sqlite3.IntegrityError.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from pydantic import BaseModel, Field
from ulid import ULID

from api.contracts import (
    ActionName,
    Approval,
    CandidateMemory,
    CandidateState,
    EventType,
    JobState,
)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_db_path: Optional[Path] = None

DEMO_DISPOSITIONS = {"interested", "not_interested", "asks_question", "no_reply"}
_TERMINAL = {CandidateState.SHORTLISTED, CandidateState.REJECTED, CandidateState.DECLINED}
_JOB_FIELDS = {"title", "form", "rubric", "shortlist_target", "score_threshold",
               "max_candidates", "permission_policy", "agent_thread_id"}
_JOB_JSON = {"form", "rubric", "permission_policy"}
_JOB_INT = {"shortlist_target", "score_threshold", "max_candidates"}
_MEMORY_FIELDS = {"profile", "research_notes", "contact", "contact_log", "decisions", "summary"}


class PermissionPolicy(BaseModel):
    """Per-job policy (specs/03-permissions.md)."""
    restricted_actions: list[ActionName] = Field(default_factory=lambda: [
        ActionName.send_outreach, ActionName.send_followup,
        ActionName.schedule_interview, ActionName.reject_candidate_proposal])
    auto_approve: bool = False
    auto_approve_actions: list[ActionName] = Field(default_factory=list)
    approval_timeout_minutes: Optional[int] = None


# ---------- plumbing ----------

def now_iso() -> str:
    d = datetime.now(timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _new_id() -> str:
    return str(ULID())


def init_db(path: Optional[str | Path] = None) -> None:
    """Create (idempotently) the schema and make `path` the active database."""
    global _db_path
    if path is None:
        from api.config import settings
        path = Path(settings.data_dir) / "recruiter.db"
    _db_path = Path(path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_db_path, timeout=30, isolation_level=None)
    try:
        conn.executescript(SCHEMA_PATH.read_text())
    finally:
        conn.close()


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """One immediate transaction; commits on success, rolls back on error."""
    if _db_path is None:
        init_db()
    conn = sqlite3.connect(_db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _dumps(v: Any) -> str:
    return json.dumps(v)


def _job_row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    for k in _JOB_JSON:
        d[k] = json.loads(d[k])
    return d


def _approval(r: sqlite3.Row) -> Approval:
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    return Approval(**d)


def _emit(conn: sqlite3.Connection, job_id: str, type: EventType | str,
          candidate_id: Optional[str] = None, payload: Optional[dict[str, Any]] = None,
          not_before: Optional[str] = None) -> int:
    et = EventType(type)
    cur = conn.execute(
        "INSERT INTO events (job_id, candidate_id, type, payload, not_before, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (job_id, candidate_id, et.value, _dumps(payload or {}), not_before, now_iso()))
    return int(cur.lastrowid)


def _candidate_row(conn: sqlite3.Connection, candidate_id: str) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    if r is None:
        raise LookupError(f"candidate {candidate_id} not found")
    return r


def _validated_policy(raw: Optional[dict[str, Any]]) -> dict[str, Any]:
    return PermissionPolicy(**(raw or {})).model_dump(mode="json")


def _check_json_dict(name: str, v: Any) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise ValueError(f"{name} must be an object")
    return v


# ---------- jobs ----------

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
    policy = _validated_policy(permission_policy)
    _check_json_dict("form", form)
    _check_json_dict("rubric", rubric or {})
    jid, ts = _new_id(), now_iso()
    with get_conn() as c:
        c.execute(
            "INSERT INTO jobs (id, title, form, rubric, state, shortlist_target, score_threshold,"
            " max_candidates, permission_policy, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (jid, title, _dumps(form), _dumps(rubric or {}), JobState.DRAFT.value,
             shortlist_target, score_threshold, max_candidates, _dumps(policy), ts, ts))
    return jid


def get_job(job_id: str) -> Optional[dict[str, Any]]:
    with get_conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _job_row(r) if r else None


def update_job(job_id: str, **fields: Any) -> None:
    """Edit rubric, thresholds, permission policy (incl. auto_approve), etc.

    `permission_policy` is merged over the stored policy, so a partial
    `{"auto_approve": True}` toggles only that key.
    """
    bad = set(fields) - _JOB_FIELDS
    if bad:
        raise ValueError(f"cannot update job fields: {sorted(bad)}")
    with get_conn() as c:
        r = c.execute("SELECT permission_policy FROM jobs WHERE id=?", (job_id,)).fetchone()
        if r is None:
            raise LookupError(f"job {job_id} not found")
        values: dict[str, Any] = {}
        for k, v in fields.items():
            if k == "permission_policy":
                v = _validated_policy({**json.loads(r["permission_policy"]), **_check_json_dict(k, v)})
            elif k in _JOB_JSON:
                v = _check_json_dict(k, v)
            elif k in _JOB_INT and (not isinstance(v, int) or isinstance(v, bool)):
                raise ValueError(f"{k} must be an integer")
            values[k] = _dumps(v) if k in _JOB_JSON else v
        values["updated_at"] = now_iso()
        sets = ", ".join(f"{k}=?" for k in values)
        c.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*values.values(), job_id))


def set_job_state(job_id: str, state: JobState) -> None:
    st = JobState(state)
    with get_conn() as c:
        cur = c.execute("UPDATE jobs SET state=?, updated_at=? WHERE id=?", (st.value, now_iso(), job_id))
        if cur.rowcount == 0:
            raise LookupError(f"job {job_id} not found")


# ---------- events ----------

def emit_event(
    job_id: str,
    type: EventType,
    candidate_id: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
    not_before: Optional[str] = None,
) -> int:
    """Insert a pending event row; return its autoincrement id."""
    et = EventType(type)  # validate before opening a transaction
    with get_conn() as c:
        return _emit(c, job_id, et, candidate_id, payload, not_before)


# ---------- candidates + memory ----------

def create_candidate(
    job_id: str,
    ploid_id: str,
    name: str,
    *,
    state: CandidateState = CandidateState.DISCOVERED,
) -> str:
    """Insert a candidate and its empty memory row; return the ULID."""
    st = CandidateState(state)
    cid, ts = _new_id(), now_iso()
    name_key = re.sub(r"[\W_]+", "", name.lower())
    with get_conn() as c:
        c.execute(
            "INSERT INTO candidates (id, job_id, ploid_id, name, name_key, state, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)", (cid, job_id, ploid_id, name, name_key, st.value, ts, ts))
        c.execute("INSERT INTO candidate_memory (candidate_id, updated_at) VALUES (?,?)", (cid, ts))
    return cid


def get_candidate(candidate_id: str) -> Optional[dict[str, Any]]:
    with get_conn() as c:
        r = c.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
    return dict(r) if r else None


def _memory(conn: sqlite3.Connection, candidate_id: str) -> CandidateMemory:
    r = conn.execute("SELECT * FROM candidate_memory WHERE candidate_id=?", (candidate_id,)).fetchone()
    if r is None:
        raise LookupError(f"memory for candidate {candidate_id} not found")
    d = dict(r)
    for k in ("profile", "research_notes", "contact", "contact_log", "decisions"):
        d[k] = json.loads(d[k])
    return CandidateMemory(**d)


def get_memory(candidate_id: str) -> CandidateMemory:
    with get_conn() as c:
        return _memory(c, candidate_id)


def _write_memory(conn: sqlite3.Connection, candidate_id: str, fields: dict[str, Any]) -> None:
    bad = set(fields) - _MEMORY_FIELDS
    if bad:
        raise ValueError(f"cannot update memory fields: {sorted(bad)}")
    merged = {**_memory(conn, candidate_id).model_dump(), **fields}
    m = CandidateMemory(**merged)  # validates types
    conn.execute(
        "UPDATE candidate_memory SET profile=?, research_notes=?, contact=?, contact_log=?,"
        " decisions=?, summary=?, updated_at=? WHERE candidate_id=?",
        (_dumps(m.profile), _dumps(m.research_notes), _dumps(m.contact), _dumps(m.contact_log),
         _dumps(m.decisions), m.summary, now_iso(), candidate_id))


def update_memory(candidate_id: str, /, **fields: Any) -> None:
    """Replace the given memory fields (others untouched), validated by Pydantic."""
    with get_conn() as c:
        _write_memory(c, candidate_id, fields)


# ---------- approvals ----------

def create_approval(
    job_id: str,
    candidate_id: str,
    action: ActionName,
    payload: Optional[dict[str, Any]] = None,
    reasoning: str = "",
    *,
    status: str = "pending",
    decided_by: Optional[str] = None,
) -> str:
    aid, ts = _new_id(), now_iso()
    a = Approval(id=aid, job_id=job_id, candidate_id=candidate_id, action=action,
                 payload=payload or {}, status=status, reasoning=reasoning, decided_by=decided_by)
    decided_at = ts if a.status != "pending" else None
    with get_conn() as c:
        c.execute(
            "INSERT INTO approvals (id, job_id, candidate_id, action, payload, status, reasoning,"
            " decided_by, created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (aid, job_id, candidate_id, a.action.value, _dumps(a.payload), a.status, a.reasoning,
             a.decided_by, ts, decided_at))
    return aid


def get_approval(approval_id: str) -> Optional[Approval]:
    with get_conn() as c:
        r = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
    return _approval(r) if r else None


def decide_approval(
    approval_id: str,
    approved: bool,
    payload: Optional[dict[str, Any]] = None,
    feedback: Optional[str] = None,
) -> None:
    """CEO decision. Approve (optionally with edited payload) -> ApprovalGranted;
    reject (with feedback) -> ApprovalRejected. Approving `send_outreach` also moves
    the candidate OUTREACH_DRAFTED -> APPROVED, atomically with the event."""
    if payload is not None:
        _check_json_dict("payload", payload)
    with get_conn() as c:
        r = c.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if r is None:
            raise LookupError(f"approval {approval_id} not found")
        if r["status"] != "pending":
            raise ValueError(f"approval {approval_id} is already {r['status']}")
        ts = now_iso()
        if approved:
            c.execute(
                "UPDATE approvals SET status='approved', decided_by='ceo', decided_at=?, payload=?,"
                " feedback=COALESCE(?, feedback) WHERE id=?",
                (ts, _dumps(payload) if payload is not None else r["payload"], feedback, approval_id))
            if r["action"] == ActionName.send_outreach.value:
                c.execute("UPDATE candidates SET state=?, updated_at=? WHERE id=? AND state=?",
                          (CandidateState.APPROVED.value, ts, r["candidate_id"],
                           CandidateState.OUTREACH_DRAFTED.value))
            _emit(c, r["job_id"], EventType.ApprovalGranted, r["candidate_id"],
                  {"approval_id": approval_id, "action": r["action"]})
        else:
            c.execute("UPDATE approvals SET status='rejected', decided_by='ceo', decided_at=?,"
                      " feedback=? WHERE id=?", (ts, feedback, approval_id))
            _emit(c, r["job_id"], EventType.ApprovalRejected, r["candidate_id"],
                  {"approval_id": approval_id, "action": r["action"], "feedback": feedback})


def mark_approval_executed(approval_id: str) -> bool:
    """Atomic approved -> executed. True for exactly one caller, ever."""
    with get_conn() as c:
        cur = c.execute("UPDATE approvals SET status='executed', executed_at=?"
                        " WHERE id=? AND status='approved'", (now_iso(), approval_id))
        return cur.rowcount == 1


# ---------- CEO overrides ----------

def resume_candidate(candidate_id: str) -> None:
    """NEEDS_HUMAN -> prev_state (or STALE -> CONTACTED); emits HumanResolved."""
    with get_conn() as c:
        r = _candidate_row(c, candidate_id)
        state = CandidateState(r["state"])
        if state == CandidateState.NEEDS_HUMAN:
            if not r["prev_state"]:
                raise ValueError(f"candidate {candidate_id} has no prev_state to resume to")
            target = CandidateState(r["prev_state"])
        elif state == CandidateState.STALE:
            target = CandidateState.CONTACTED
        else:
            raise ValueError(f"cannot resume a candidate in {state.value}")
        c.execute("UPDATE candidates SET state=?, prev_state=NULL, updated_at=? WHERE id=?",
                  (target.value, now_iso(), candidate_id))
        _emit(c, r["job_id"], EventType.HumanResolved, candidate_id,
              {"from": state.value, "resumed_to": target.value})


def skip_candidate(candidate_id: str) -> None:
    """CEO override: move to a terminal state (REJECTED) without agent action (logged)."""
    with get_conn() as c:
        r = _candidate_row(c, candidate_id)
        state = CandidateState(r["state"])
        if state in _TERMINAL:
            raise ValueError(f"candidate {candidate_id} is already terminal ({state.value})")
        ts = now_iso()
        c.execute("UPDATE candidates SET state=?, prev_state=NULL, updated_at=? WHERE id=?",
                  (CandidateState.REJECTED.value, ts, candidate_id))
        decisions = _memory(c, candidate_id).decisions + [{
            "ts": ts, "action": "skip_candidate", "reasoning": "CEO skipped this candidate",
            "outcome": f"{state.value} -> REJECTED"}]
        _write_memory(c, candidate_id, {"decisions": decisions})


def set_demo_disposition(candidate_id: str, disposition: Optional[str]) -> None:
    """Pin the persona reply: interested | not_interested | asks_question | no_reply."""
    if disposition is not None and disposition not in DEMO_DISPOSITIONS:
        raise ValueError(f"disposition must be one of {sorted(DEMO_DISPOSITIONS)} or None")
    with get_conn() as c:
        _candidate_row(c, candidate_id)
        c.execute("UPDATE candidates SET demo_disposition=?, updated_at=? WHERE id=?",
                  (disposition, now_iso(), candidate_id))


# ---------- per-candidate lock ----------

def acquire_lock(candidate_id: str, owner: str, stale_after_s: int = 120) -> bool:
    """Take the lock if free, already ours, or stale (crashed worker)."""
    with get_conn() as c:
        r = _candidate_row(c, candidate_id)
        held = r["locked_by"] is not None and r["locked_by"] != owner
        if held and r["locked_at"]:
            held = _parse_ts(r["locked_at"]) > datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)
        if held:
            return False
        c.execute("UPDATE candidates SET locked_by=?, locked_at=? WHERE id=?",
                  (owner, now_iso(), candidate_id))
        return True


def release_lock(candidate_id: str, owner: str) -> None:
    with get_conn() as c:
        c.execute("UPDATE candidates SET locked_by=NULL, locked_at=NULL WHERE id=? AND locked_by=?",
                  (candidate_id, owner))
