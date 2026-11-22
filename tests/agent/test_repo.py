"""Red tests for the api/db/repo.py implementations (lane A, 0:20-1:10).

Behavior source: specs/01 (lock), 03 (approvals/policy), 04 (data model).
Assumed API beyond the kickoff stubs (implement to match):
    init_db(path=None)                 create/upgrade the DB file and make it the active one
    get_conn()                         context manager -> sqlite3.Connection (row_factory=Row,
                                       foreign_keys=ON, WAL), commits on exit, rolls back on error
    get_job(job_id) -> dict | None     JSON columns parsed
    get_candidate(cid) -> dict | None
    create_candidate(job_id, ploid_id, name, *, state=CandidateState.DISCOVERED) -> id
                                       also inserts an empty candidate_memory row
    get_memory(cid) -> CandidateMemory
    update_memory(cid, **fields)       replaces the given memory fields, validated by Pydantic
    create_approval(job_id, cid, action, payload=None, reasoning="", *, status="pending",
                    decided_by=None) -> id
    get_approval(aid) -> Approval | None
    mark_approval_executed(aid) -> bool    atomic approved -> executed, True at most once
    acquire_lock(cid, owner, stale_after_s=120) -> bool
    release_lock(cid, owner) -> None
Errors: unknown ids raise LookupError; illegal values / state conflicts raise ValueError
(pydantic ValidationError counts); FK violations raise sqlite3.IntegrityError.
"""
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from api.contracts import (
    ActionName as A,
    Approval,
    CandidateMemory,
    CandidateState as S,
    EventType as E,
    JobState,
)
from api.db import repo

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


@pytest.fixture
def db(tmp_path):
    repo.init_db(tmp_path / "recruiter.db")
    return repo


@pytest.fixture
def job(db):
    return db.create_job("Founding Engineer", {"must_haves": ["Python"]})


@pytest.fixture
def cand(db, job):
    return db.create_candidate(job, "p-1", "Sarah Chen")


def rows(sql, *args):
    with repo.get_conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def events_for(job_id, type=None):
    out = rows("SELECT * FROM events WHERE job_id=? ORDER BY id", job_id)
    return [e for e in out if type is None or e["type"] == type.value]


def assert_iso_utc(ts):
    assert isinstance(ts, str) and ts.endswith("Z")
    datetime.fromisoformat(ts.replace("Z", "+00:00"))


def make_approval(db, job, cand, action=A.send_outreach, payload=None, **kw):
    return db.create_approval(job, cand, action, payload or {"subject": "Hi", "body": "Hello"},
                              "fits the role", **kw)


# ---------- connection / schema ----------

def test_init_db_is_idempotent_and_creates_tables(db, tmp_path):
    repo.init_db(tmp_path / "recruiter.db")
    names = {r["name"] for r in rows("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "candidates", "candidate_memory", "events", "approvals", "tool_calls"} <= names


def test_connection_enforces_foreign_keys_and_wal(db):
    with repo.get_conn() as c:
        assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_get_conn_rolls_back_on_error(db, job):
    with pytest.raises(RuntimeError):
        with repo.get_conn() as c:
            c.execute("UPDATE jobs SET title='changed' WHERE id=?", (job,))
            raise RuntimeError("boom")
    assert db.get_job(job)["title"] == "Founding Engineer"


# ---------- create_job ----------

def test_create_job_returns_ulid_and_draft_defaults(db):
    jid = db.create_job("Role", {"location": "Toronto"})
    assert ULID_RE.match(jid)
    j = db.get_job(jid)
    assert j["state"] == JobState.DRAFT.value
    assert j["form"] == {"location": "Toronto"}
    assert j["rubric"] == {}
    assert (j["shortlist_target"], j["score_threshold"], j["max_candidates"]) == (3, 75, 5)
    assert_iso_utc(j["created_at"])
    assert_iso_utc(j["updated_at"])


def test_create_job_default_permission_policy_matches_spec_03(db, job):
    p = db.get_job(job)["permission_policy"]
    assert p["auto_approve"] is False
    assert p["auto_approve_actions"] == []
    assert p["approval_timeout_minutes"] is None
    assert set(p["restricted_actions"]) == {"send_outreach", "send_followup",
                                            "schedule_interview", "reject_candidate_proposal"}


def test_create_job_stores_custom_values(db):
    jid = db.create_job("R", {}, rubric={"criteria": [{"name": "x", "weight": 1}]},
                        permission_policy={"auto_approve": True}, shortlist_target=2,
                        score_threshold=80, max_candidates=9)
    j = db.get_job(jid)
    assert j["rubric"]["criteria"][0]["name"] == "x"
    assert j["permission_policy"]["auto_approve"] is True
    assert j["permission_policy"]["restricted_actions"]  # defaults are filled in
    assert (j["shortlist_target"], j["score_threshold"], j["max_candidates"]) == (2, 80, 9)


def test_create_job_ids_are_unique(db):
    assert len({db.create_job("R", {}) for _ in range(20)}) == 20


@pytest.mark.parametrize("policy", [
    {"auto_approve_actions": "send_outreach"},          # not a list
    {"restricted_actions": ["launch_missiles"]},        # unknown action
    {"auto_approve": "perhaps"},                        # not a bool
])
def test_create_job_rejects_invalid_policy(db, policy):
    with pytest.raises(ValueError):
        db.create_job("R", {}, permission_policy=policy)
    assert rows("SELECT * FROM jobs") == []


# ---------- get_job / get_candidate ----------

def test_get_missing_returns_none(db):
    assert db.get_job("nope") is None
    assert db.get_candidate("nope") is None
    assert db.get_approval("nope") is None


# ---------- update_job ----------

def test_update_job_edits_fields_and_bumps_updated_at(db, job):
    before = db.get_job(job)["updated_at"]
    db.update_job(job, title="Staff Engineer", score_threshold=60, rubric={"k": 1})
    j = db.get_job(job)
    assert (j["title"], j["score_threshold"], j["rubric"]) == ("Staff Engineer", 60, {"k": 1})
    assert j["updated_at"] >= before


def test_update_job_toggles_auto_approve_keeping_policy_defaults(db, job):
    db.update_job(job, permission_policy={"auto_approve": True})
    p = db.get_job(job)["permission_policy"]
    assert p["auto_approve"] is True
    assert "send_outreach" in p["restricted_actions"]


def test_update_job_rejects_unknown_field(db, job):
    with pytest.raises(ValueError):
        db.update_job(job, id="other")
    with pytest.raises(ValueError):
        db.update_job(job, nonsense=1)


def test_update_job_rejects_invalid_policy_without_partial_write(db, job):
    with pytest.raises(ValueError):
        db.update_job(job, title="New", permission_policy={"auto_approve_actions": "x"})
    assert db.get_job(job)["title"] == "Founding Engineer"


def test_update_job_unknown_job(db):
    with pytest.raises(LookupError):
        db.update_job("missing", title="x")


# ---------- set_job_state ----------

@pytest.mark.parametrize("state", list(JobState), ids=lambda s: s.value)
def test_set_job_state(db, job, state):
    db.set_job_state(job, state)
    assert db.get_job(job)["state"] == state.value


def test_set_job_state_accepts_string_and_rejects_garbage(db, job):
    db.set_job_state(job, "ACTIVE")
    assert db.get_job(job)["state"] == "ACTIVE"
    with pytest.raises(ValueError):
        db.set_job_state(job, "BOGUS")
    assert db.get_job(job)["state"] == "ACTIVE"


def test_set_job_state_unknown_job(db):
    with pytest.raises(LookupError):
        db.set_job_state("missing", JobState.ACTIVE)


# ---------- emit_event ----------

def test_emit_event_inserts_pending_row(db, job):
    eid = db.emit_event(job, E.JobCreated)
    assert isinstance(eid, int)
    (ev,) = events_for(job)
    assert ev["id"] == eid
    assert ev["type"] == "JobCreated"
    assert ev["status"] == "pending"
    assert ev["attempts"] == 0
    assert ev["candidate_id"] is None
    assert ev["not_before"] is None
    assert ev["processed_at"] is None
    assert_iso_utc(ev["created_at"])


def test_emit_event_ids_increase(db, job):
    ids = [db.emit_event(job, E.Tick) for _ in range(5)]
    assert ids == sorted(ids) and len(set(ids)) == 5


def test_emit_event_stores_payload_candidate_and_not_before(db, job, cand):
    nb = (datetime.now(timezone.utc) + timedelta(seconds=15)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    db.emit_event(job, E.EmailReplied, candidate_id=cand, payload={"disposition": "interested"},
                  not_before=nb)
    (ev,) = events_for(job)
    assert ev["candidate_id"] == cand
    assert ev["not_before"] == nb
    import json
    assert json.loads(ev["payload"]) == {"disposition": "interested"}


def test_emit_event_accepts_string_type_but_rejects_unknown(db, job):
    db.emit_event(job, "Tick")
    with pytest.raises(ValueError):
        db.emit_event(job, "NotAnEvent")


def test_emit_event_requires_existing_job_and_candidate(db, job):
    with pytest.raises(sqlite3.IntegrityError):
        db.emit_event("missing", E.Tick)
    with pytest.raises(sqlite3.IntegrityError):
        db.emit_event(job, E.Tick, candidate_id="missing")


def test_emit_event_is_safe_across_threads(db, job):
    ids, errors = [], []

    def work():
        try:
            for _ in range(10):
                ids.append(db.emit_event(job, E.Tick))
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    ts = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors
    assert len(set(ids)) == 60
    assert len(events_for(job)) == 60


# ---------- create_candidate / memory ----------

def test_create_candidate_defaults_and_memory_row(db, job):
    cid = db.create_candidate(job, "p-9", "Dr. José  O'Neil-Smith")
    assert ULID_RE.match(cid)
    c = db.get_candidate(cid)
    assert c["state"] == "DISCOVERED"
    assert c["job_id"] == job and c["ploid_id"] == "p-9"
    assert c["name_key"] == "drjoséoneilsmith"  # lowercase, whitespace/punctuation stripped
    assert c["locked_by"] is None and c["prev_state"] is None and c["score"] is None
    m = db.get_memory(cid)
    assert isinstance(m, CandidateMemory)
    assert (m.profile, m.contact_log, m.decisions, m.summary) == ({}, [], [], "")


def test_name_key_is_normalized_for_duplicate_guard(db, job):
    a = db.get_candidate(db.create_candidate(job, "p-a", "Sarah  Chen"))["name_key"]
    b = db.get_candidate(db.create_candidate(job, "p-b", "sarah chen!"))["name_key"]
    assert a == b == "sarahchen"


def test_create_candidate_unique_per_job_ploid(db, job):
    db.create_candidate(job, "p-1", "A")
    with pytest.raises(sqlite3.IntegrityError):
        db.create_candidate(job, "p-1", "A again")
    other = db.create_job("Other", {})
    db.create_candidate(other, "p-1", "A")  # same ploid, different job is fine


def test_create_candidate_with_initial_state(db, job):
    cid = db.create_candidate(job, "p-2", "B", state=S.CONTACTED)
    assert db.get_candidate(cid)["state"] == "CONTACTED"


def test_update_memory_replaces_given_fields_only(db, cand):
    db.update_memory(cand, profile={"skills": ["React"]}, summary="Strong frontend")
    db.update_memory(cand, contact={"email": "s@example.com"})
    m = db.get_memory(cand)
    assert m.profile == {"skills": ["React"]}
    assert m.summary == "Strong frontend"
    assert m.contact == {"email": "s@example.com"}
    assert m.decisions == []
    assert_iso_utc(m.updated_at)


def test_update_memory_validates(db, cand):
    with pytest.raises(ValueError):
        db.update_memory(cand, contact_log="not a list")
    with pytest.raises(ValueError):
        db.update_memory(cand, candidate_id="other")
    with pytest.raises(ValueError):
        db.update_memory(cand, bogus=1)
    assert db.get_memory(cand).contact_log == []


def test_memory_round_trips_decisions_and_log(db, cand):
    d = [{"ts": "2026-11-22T00:00:00Z", "action": "enrich_candidate", "reasoning": "r", "outcome": "ok"}]
    log = [{"ts": "2026-11-22T00:01:00Z", "channel": "email", "action": "send_outreach", "message_id": "m1"}]
    db.update_memory(cand, decisions=d, contact_log=log)
    m = db.get_memory(cand)
    assert m.decisions == d and m.contact_log == log


def test_get_memory_unknown_candidate(db):
    with pytest.raises(LookupError):
        db.get_memory("missing")


# ---------- approvals ----------

def test_create_and_get_approval(db, job, cand):
    aid = make_approval(db, job, cand)
    assert ULID_RE.match(aid)
    a = db.get_approval(aid)
    assert isinstance(a, Approval)
    assert a.status == "pending" and a.action == A.send_outreach
    assert a.payload == {"subject": "Hi", "body": "Hello"}
    assert a.reasoning == "fits the role"
    assert a.decided_by is None and a.decided_at is None and a.executed_at is None
    assert_iso_utc(a.created_at)


def test_create_approval_rejects_unknown_action(db, job, cand):
    with pytest.raises(ValueError):
        db.create_approval(job, cand, "launch_missiles", {}, "")


def test_approve_marks_approved_by_ceo_and_emits_granted(db, job, cand):
    aid = make_approval(db, job, cand)
    db.decide_approval(aid, True)
    a = db.get_approval(aid)
    assert a.status == "approved" and a.decided_by == "ceo"
    assert_iso_utc(a.decided_at)
    (ev,) = events_for(job, E.ApprovalGranted)
    assert ev["candidate_id"] == cand and ev["status"] == "pending"
    import json
    assert json.loads(ev["payload"])["approval_id"] == aid


def test_approve_with_edited_payload_stores_the_edit(db, job, cand):
    aid = make_approval(db, job, cand)
    db.decide_approval(aid, True, payload={"subject": "Edited", "body": "New body"})
    assert db.get_approval(aid).payload == {"subject": "Edited", "body": "New body"}


def test_approve_without_payload_keeps_original(db, job, cand):
    aid = make_approval(db, job, cand)
    db.decide_approval(aid, True)
    assert db.get_approval(aid).payload == {"subject": "Hi", "body": "Hello"}


def test_approving_send_outreach_moves_candidate_to_approved(db, job):
    cid = db.create_candidate(job, "p-3", "C", state=S.OUTREACH_DRAFTED)
    db.decide_approval(make_approval(db, job, cid), True)
    assert db.get_candidate(cid)["state"] == "APPROVED"


def test_approving_other_actions_does_not_change_state(db, job):
    cid = db.create_candidate(job, "p-4", "D", state=S.INTERESTED)
    db.decide_approval(make_approval(db, job, cid, action=A.schedule_interview, payload={"q": []}), True)
    assert db.get_candidate(cid)["state"] == "INTERESTED"


def test_reject_stores_feedback_and_emits_rejected(db, job, cand):
    aid = make_approval(db, job, cand)
    db.decide_approval(aid, False, feedback="Too formal")
    a = db.get_approval(aid)
    assert a.status == "rejected" and a.feedback == "Too formal" and a.decided_by == "ceo"
    assert_iso_utc(a.decided_at)
    (ev,) = events_for(job, E.ApprovalRejected)
    import json
    body = json.loads(ev["payload"])
    assert body["approval_id"] == aid and body["feedback"] == "Too formal"
    assert events_for(job, E.ApprovalGranted) == []


def test_reject_does_not_change_candidate_state(db, job):
    cid = db.create_candidate(job, "p-5", "E", state=S.OUTREACH_DRAFTED)
    db.decide_approval(make_approval(db, job, cid), False, feedback="no")
    assert db.get_candidate(cid)["state"] == "OUTREACH_DRAFTED"


def test_cannot_decide_an_approval_twice(db, job, cand):
    aid = make_approval(db, job, cand)
    db.decide_approval(aid, True)
    with pytest.raises(ValueError):
        db.decide_approval(aid, False, feedback="changed my mind")
    with pytest.raises(ValueError):
        db.decide_approval(aid, True)
    assert db.get_approval(aid).status == "approved"
    assert len(events_for(job, E.ApprovalGranted)) == 1
    assert events_for(job, E.ApprovalRejected) == []


def test_decide_unknown_approval(db):
    with pytest.raises(LookupError):
        db.decide_approval("missing", True)


def test_mark_approval_executed_is_at_most_once(db, job, cand):
    aid = make_approval(db, job, cand)
    db.decide_approval(aid, True)
    assert db.mark_approval_executed(aid) is True
    assert db.mark_approval_executed(aid) is False
    a = db.get_approval(aid)
    assert a.status == "executed"
    assert_iso_utc(a.executed_at)


def test_mark_approval_executed_refuses_unapproved(db, job, cand):
    pending = make_approval(db, job, cand)
    assert db.mark_approval_executed(pending) is False
    assert db.get_approval(pending).status == "pending"
    rejected = make_approval(db, job, cand)
    db.decide_approval(rejected, False, feedback="no")
    assert db.mark_approval_executed(rejected) is False


def test_concurrent_execution_claims_exactly_once(db, job, cand):
    aid = make_approval(db, job, cand)
    db.decide_approval(aid, True)
    wins = []
    ts = [threading.Thread(target=lambda: wins.append(db.mark_approval_executed(aid))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins.count(True) == 1


def test_auto_approved_row_can_be_created_approved(db, job, cand):
    aid = make_approval(db, job, cand, status="approved", decided_by="auto")
    a = db.get_approval(aid)
    assert a.status == "approved" and a.decided_by == "auto"
    assert db.mark_approval_executed(aid) is True


# ---------- resume_candidate ----------

def test_resume_needs_human_returns_to_prev_state(db, job):
    cid = db.create_candidate(job, "p-6", "F", state=S.NEEDS_HUMAN)
    with repo.get_conn() as c:
        c.execute("UPDATE candidates SET prev_state='CONTACT_FOUND' WHERE id=?", (cid,))
    db.resume_candidate(cid)
    c = db.get_candidate(cid)
    assert c["state"] == "CONTACT_FOUND" and c["prev_state"] is None
    (ev,) = events_for(job, E.HumanResolved)
    assert ev["candidate_id"] == cid and ev["status"] == "pending"


def test_resume_stale_goes_to_contacted(db, job):
    cid = db.create_candidate(job, "p-7", "G", state=S.STALE)
    db.resume_candidate(cid)
    assert db.get_candidate(cid)["state"] == "CONTACTED"
    assert len(events_for(job, E.HumanResolved)) == 1


def test_resume_needs_human_without_prev_state_is_an_error(db, job):
    cid = db.create_candidate(job, "p-8", "H", state=S.NEEDS_HUMAN)
    with pytest.raises(ValueError):
        db.resume_candidate(cid)
    assert db.get_candidate(cid)["state"] == "NEEDS_HUMAN"


@pytest.mark.parametrize("state", [S.DISCOVERED, S.CONTACTED, S.SHORTLISTED, S.REJECTED],
                         ids=lambda s: s.value)
def test_resume_rejects_states_that_cannot_resume(db, job, state):
    cid = db.create_candidate(job, f"p-{state.value}", "X", state=state)
    with pytest.raises(ValueError):
        db.resume_candidate(cid)
    assert db.get_candidate(cid)["state"] == state.value
    assert events_for(job, E.HumanResolved) == []


def test_resume_unknown_candidate(db):
    with pytest.raises(LookupError):
        db.resume_candidate("missing")


# ---------- skip_candidate ----------

def test_skip_moves_to_terminal_and_logs_decision(db, job, cand):
    db.skip_candidate(cand)
    assert db.get_candidate(cand)["state"] == "REJECTED"
    log = db.get_memory(cand).decisions
    assert len(log) == 1
    assert "skip" in log[0]["action"]
    assert_iso_utc(log[0]["ts"])


def test_skip_from_needs_human_clears_prev_state(db, job):
    cid = db.create_candidate(job, "p-s", "S", state=S.NEEDS_HUMAN)
    with repo.get_conn() as c:
        c.execute("UPDATE candidates SET prev_state='RESEARCHED' WHERE id=?", (cid,))
    db.skip_candidate(cid)
    c = db.get_candidate(cid)
    assert c["state"] == "REJECTED" and c["prev_state"] is None


@pytest.mark.parametrize("state", [S.SHORTLISTED, S.REJECTED, S.DECLINED], ids=lambda s: s.value)
def test_skip_on_terminal_candidate_is_an_error(db, job, state):
    cid = db.create_candidate(job, f"t-{state.value}", "T", state=state)
    with pytest.raises(ValueError):
        db.skip_candidate(cid)
    assert db.get_candidate(cid)["state"] == state.value


def test_skip_unknown_candidate(db):
    with pytest.raises(LookupError):
        db.skip_candidate("missing")


# ---------- set_demo_disposition ----------

@pytest.mark.parametrize("d", ["interested", "not_interested", "asks_question", "no_reply"])
def test_set_demo_disposition(db, cand, d):
    db.set_demo_disposition(cand, d)
    assert db.get_candidate(cand)["demo_disposition"] == d


def test_clear_demo_disposition(db, cand):
    db.set_demo_disposition(cand, "interested")
    db.set_demo_disposition(cand, None)
    assert db.get_candidate(cand)["demo_disposition"] is None


def test_invalid_demo_disposition(db, cand):
    with pytest.raises(ValueError):
        db.set_demo_disposition(cand, "maybe")
    assert db.get_candidate(cand)["demo_disposition"] is None


def test_set_demo_disposition_unknown_candidate(db):
    with pytest.raises(LookupError):
        db.set_demo_disposition("missing", "interested")


# ---------- per-candidate lock ----------

def test_lock_is_exclusive_between_owners(db, cand):
    assert db.acquire_lock(cand, "worker-1") is True
    assert db.acquire_lock(cand, "worker-2") is False
    c = db.get_candidate(cand)
    assert c["locked_by"] == "worker-1"
    assert_iso_utc(c["locked_at"])


def test_lock_is_reentrant_for_same_owner(db, cand):
    assert db.acquire_lock(cand, "w") is True
    assert db.acquire_lock(cand, "w") is True


def test_release_only_by_owner(db, cand):
    db.acquire_lock(cand, "w1")
    db.release_lock(cand, "w2")
    assert db.get_candidate(cand)["locked_by"] == "w1"
    db.release_lock(cand, "w1")
    c = db.get_candidate(cand)
    assert c["locked_by"] is None and c["locked_at"] is None
    assert db.acquire_lock(cand, "w2") is True


def test_stale_lock_can_be_taken_over(db, cand):
    db.acquire_lock(cand, "dead-worker")
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with repo.get_conn() as c:
        c.execute("UPDATE candidates SET locked_at=? WHERE id=?", (old, cand))
    assert db.acquire_lock(cand, "w2") is True
    assert db.get_candidate(cand)["locked_by"] == "w2"


def test_fresh_lock_is_not_taken_over(db, cand):
    db.acquire_lock(cand, "w1")
    assert db.acquire_lock(cand, "w2", stale_after_s=120) is False


def test_lock_race_has_one_winner(db, cand):
    wins = []
    ts = [threading.Thread(target=lambda i=i: wins.append(db.acquire_lock(cand, f"w{i}")))
          for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins.count(True) == 1


def test_lock_unknown_candidate(db):
    with pytest.raises(LookupError):
        db.acquire_lock("missing", "w")
