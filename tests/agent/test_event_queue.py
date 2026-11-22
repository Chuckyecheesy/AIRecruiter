"""Red tests for the event-queue functions in repo.py that the dispatcher builds on.

Source: specs/05-agent-graph.md ("Event dispatcher"), specs/04-data-model.md (events).
Assumed API (implement to match):
    fetch_due_events(limit=10, job_id=None) -> list[Event]
        status='pending' AND (not_before IS NULL OR not_before <= now), ordered by id,
        only for ACTIVE jobs (PAUSED/DRAFT/COMPLETE stay pending), optional job filter.
    claim_event(event_id) -> bool
        atomic pending -> processing; True for exactly one caller.
        `processed_at` doubles as the claim time while status='processing'.
    mark_event_done(event_id) -> None
    mark_event_failed(event_id, max_attempts=3) -> bool
        attempts += 1. Under max_attempts: back to 'pending' (retry), returns False.
        At max_attempts: status='failed', returns True (caller escalates).
    reset_stuck_events(stale_after_s=120) -> int
        crash recovery: 'processing' older than the threshold -> 'pending'; returns count.
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from api.contracts import Event, EventType as E, JobState
from api.db import repo


@pytest.fixture
def db(tmp_path):
    repo.init_db(tmp_path / "recruiter.db")
    return repo


@pytest.fixture
def job(db):
    jid = db.create_job("Role", {})
    db.set_job_state(jid, JobState.ACTIVE)
    return jid


@pytest.fixture
def cand(db, job):
    return db.create_candidate(job, "p-1", "Sarah Chen")


def ts(delta_s):
    d = datetime.now(timezone.utc) + timedelta(seconds=delta_s)
    return d.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def event_row(eid):
    with repo.get_conn() as c:
        return dict(c.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())


def set_event(eid, **cols):
    sets = ", ".join(f"{k}=?" for k in cols)
    with repo.get_conn() as c:
        c.execute(f"UPDATE events SET {sets} WHERE id=?", (*cols.values(), eid))


# ---------- fetch_due_events ----------

def test_fetch_returns_pending_events_in_id_order(db, job, cand):
    ids = [db.emit_event(job, E.Tick, candidate_id=cand) for _ in range(4)]
    got = db.fetch_due_events()
    assert [e.id for e in got] == ids
    assert all(isinstance(e, Event) for e in got)


def test_fetch_parses_payload_and_fields(db, job, cand):
    db.emit_event(job, E.EmailReplied, candidate_id=cand, payload={"disposition": "interested"})
    (e,) = db.fetch_due_events()
    assert e.type == E.EmailReplied and e.job_id == job and e.candidate_id == cand
    assert e.payload == {"disposition": "interested"}
    assert e.status == "pending" and e.attempts == 0


def test_fetch_respects_not_before(db, job, cand):
    future = db.emit_event(job, E.Tick, candidate_id=cand, not_before=ts(+60))
    past = db.emit_event(job, E.Tick, candidate_id=cand, not_before=ts(-60))
    none = db.emit_event(job, E.Tick, candidate_id=cand)
    assert [e.id for e in db.fetch_due_events()] == [past, none]
    assert future not in [e.id for e in db.fetch_due_events()]


def test_fetch_honors_limit_and_returns_oldest_first(db, job, cand):
    ids = [db.emit_event(job, E.Tick, candidate_id=cand) for _ in range(15)]
    assert [e.id for e in db.fetch_due_events(limit=10)] == ids[:10]
    assert [e.id for e in db.fetch_due_events(limit=3)] == ids[:3]


def test_fetch_skips_non_pending(db, job, cand):
    a = db.emit_event(job, E.Tick, candidate_id=cand)
    b = db.emit_event(job, E.Tick, candidate_id=cand)
    c = db.emit_event(job, E.Tick, candidate_id=cand)
    set_event(a, status="processing")
    set_event(b, status="done")
    assert [e.id for e in db.fetch_due_events()] == [c]


@pytest.mark.parametrize("state", [JobState.PAUSED, JobState.DRAFT, JobState.COMPLETE],
                         ids=lambda s: s.value)
def test_fetch_skips_events_of_inactive_jobs(db, job, cand, state):
    eid = db.emit_event(job, E.Tick, candidate_id=cand)
    db.set_job_state(job, state)
    assert db.fetch_due_events() == []
    db.set_job_state(job, JobState.ACTIVE)  # events stayed pending
    assert [e.id for e in db.fetch_due_events()] == [eid]


def test_fetch_job_filter(db, job, cand):
    other = db.create_job("Other", {})
    db.set_job_state(other, JobState.ACTIVE)
    mine = db.emit_event(job, E.Tick, candidate_id=cand)
    db.emit_event(other, E.JobCreated)
    assert [e.id for e in db.fetch_due_events(job_id=job)] == [mine]
    assert len(db.fetch_due_events()) == 2


def test_fetch_includes_job_level_events(db, job):
    eid = db.emit_event(job, E.JobCreated)
    (e,) = db.fetch_due_events()
    assert e.id == eid and e.candidate_id is None


# ---------- claim_event ----------

def test_claim_moves_pending_to_processing(db, job, cand):
    eid = db.emit_event(job, E.Tick, candidate_id=cand)
    assert db.claim_event(eid) is True
    row = event_row(eid)
    assert row["status"] == "processing"
    assert row["processed_at"] is not None  # claim time
    assert db.fetch_due_events() == []


def test_claim_is_exclusive(db, job, cand):
    eid = db.emit_event(job, E.Tick, candidate_id=cand)
    assert db.claim_event(eid) is True
    assert db.claim_event(eid) is False


@pytest.mark.parametrize("status", ["processing", "done", "failed"])
def test_claim_refuses_non_pending(db, job, cand, status):
    eid = db.emit_event(job, E.Tick, candidate_id=cand)
    set_event(eid, status=status)
    assert db.claim_event(eid) is False
    assert event_row(eid)["status"] == status


def test_claim_unknown_event_is_false(db):
    assert db.claim_event(999999) is False


def test_concurrent_claims_have_one_winner(db, job, cand):
    eid = db.emit_event(job, E.Tick, candidate_id=cand)
    wins = []
    ts_ = [threading.Thread(target=lambda: wins.append(db.claim_event(eid))) for _ in range(8)]
    [t.start() for t in ts_]
    [t.join() for t in ts_]
    assert wins.count(True) == 1


# ---------- mark done / failed ----------

def test_mark_event_done(db, job, cand):
    eid = db.emit_event(job, E.Tick, candidate_id=cand)
    db.claim_event(eid)
    db.mark_event_done(eid)
    row = event_row(eid)
    assert row["status"] == "done" and row["processed_at"].endswith("Z")


def test_failed_event_retries_until_max_attempts(db, job, cand):
    eid = db.emit_event(job, E.Tick, candidate_id=cand)
    results = []
    for _ in range(3):
        assert db.claim_event(eid) is True
        results.append(db.mark_event_failed(eid))
    assert results == [False, False, True]
    row = event_row(eid)
    assert row["status"] == "failed" and row["attempts"] == 3


def test_failed_event_goes_back_to_pending_under_the_limit(db, job, cand):
    eid = db.emit_event(job, E.Tick, candidate_id=cand)
    db.claim_event(eid)
    assert db.mark_event_failed(eid) is False
    row = event_row(eid)
    assert row["status"] == "pending" and row["attempts"] == 1
    assert [e.id for e in db.fetch_due_events()] == [eid]


def test_mark_event_failed_custom_max_attempts(db, job, cand):
    eid = db.emit_event(job, E.Tick, candidate_id=cand)
    db.claim_event(eid)
    assert db.mark_event_failed(eid, max_attempts=1) is True
    assert event_row(eid)["status"] == "failed"


def test_failed_events_are_not_refetched(db, job, cand):
    eid = db.emit_event(job, E.Tick, candidate_id=cand)
    db.claim_event(eid)
    db.mark_event_failed(eid, max_attempts=1)
    assert db.fetch_due_events() == []


# ---------- reset_stuck_events (crash recovery) ----------

def test_reset_stuck_events_resets_only_old_processing_rows(db, job, cand):
    old = db.emit_event(job, E.Tick, candidate_id=cand)
    fresh = db.emit_event(job, E.Tick, candidate_id=cand)
    done = db.emit_event(job, E.Tick, candidate_id=cand)
    pending = db.emit_event(job, E.Tick, candidate_id=cand)
    set_event(old, status="processing", processed_at=ts(-300))
    set_event(fresh, status="processing", processed_at=ts(-10))
    set_event(done, status="done", processed_at=ts(-300))
    assert db.reset_stuck_events(stale_after_s=120) == 1
    assert event_row(old)["status"] == "pending"
    assert event_row(fresh)["status"] == "processing"
    assert event_row(done)["status"] == "done"
    assert event_row(pending)["status"] == "pending"


def test_reset_stuck_events_makes_them_fetchable_again(db, job, cand):
    eid = db.emit_event(job, E.Tick, candidate_id=cand)
    db.claim_event(eid)
    set_event(eid, processed_at=ts(-600))
    db.reset_stuck_events()
    assert [e.id for e in db.fetch_due_events()] == [eid]


def test_reset_stuck_events_treats_missing_claim_time_as_stale(db, job, cand):
    eid = db.emit_event(job, E.Tick, candidate_id=cand)
    set_event(eid, status="processing", processed_at=None)
    assert db.reset_stuck_events() == 1


def test_reset_stuck_events_returns_zero_when_clean(db, job, cand):
    db.emit_event(job, E.Tick, candidate_id=cand)
    assert db.reset_stuck_events() == 0
