"""Red tests for api/db/schema.sql (lane A kickoff). Source: specs/04-data-model.md."""
import sqlite3
from pathlib import Path

import pytest

SCHEMA = Path(__file__).resolve().parents[2] / "api" / "db" / "schema.sql"

EXPECTED_COLUMNS = {
    "jobs": {"id", "title", "form", "rubric", "state", "shortlist_target", "score_threshold",
             "max_candidates", "permission_policy", "agent_thread_id", "created_at", "updated_at"},
    "candidates": {"id", "job_id", "ploid_id", "name", "name_key", "state", "prev_state", "score",
                   "demo_disposition", "locked_by", "locked_at", "trace_url", "created_at", "updated_at"},
    "candidate_memory": {"candidate_id", "profile", "research_notes", "contact", "contact_log",
                         "decisions", "summary", "updated_at"},
    "messages": {"id", "candidate_id", "job_id", "channel", "direction", "subject", "body",
                 "thread_id", "simulated", "created_at"},
    "interviews": {"id", "candidate_id", "job_id", "conversation_id", "join_url", "expires_at",
                   "questions", "status", "transcript", "duration_s", "created_at", "completed_at"},
    "evaluations": {"id", "candidate_id", "job_id", "score", "criteria", "recommendation",
                    "summary", "created_at"},
    "approvals": {"id", "job_id", "candidate_id", "action", "payload", "status", "reasoning",
                  "decided_by", "feedback", "created_at", "decided_at", "executed_at"},
    "events": {"id", "job_id", "candidate_id", "type", "payload", "status", "not_before",
               "attempts", "created_at", "processed_at"},
    "tool_calls": {"id", "run_id", "candidate_id", "tool", "action", "idempotency_key", "input",
                   "output", "status", "error_kind", "attempts", "latency_ms", "langfuse_span_id",
                   "created_at"},
    "agent_runs": {"id", "job_id", "candidate_id", "event_id", "langfuse_trace_id", "trace_url",
                   "status", "chosen_action", "error", "started_at", "ended_at"},
}


@pytest.fixture
def db(tmp_path):
    assert SCHEMA.exists(), f"missing {SCHEMA}"
    conn = sqlite3.connect(tmp_path / "recruiter.db")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA.read_text())
    yield conn
    conn.close()


def cols(db, table):
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def indexes(db, table):
    out = []
    for _, name, unique, *_ in db.execute(f"PRAGMA index_list({table})"):
        cs = [r[2] for r in db.execute(f"PRAGMA index_info({name})")]
        out.append((tuple(cs), bool(unique)))
    return out


def seed_job(db, jid="J1"):
    db.execute("INSERT INTO jobs (id, title, state) VALUES (?, 'Eng', 'ACTIVE')", (jid,))


def seed_candidate(db, cid="C1", jid="J1", ploid="P1"):
    db.execute(
        "INSERT INTO candidates (id, job_id, ploid_id, name, name_key, state) "
        "VALUES (?, ?, ?, 'Sarah', 'sarah', 'DISCOVERED')", (cid, jid, ploid))


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_table_has_exactly_the_spec_columns(db, table):
    assert cols(db, table) == EXPECTED_COLUMNS[table]


def test_wal_mode_enabled(db):
    assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_schema_is_idempotent(db):
    db.executescript(SCHEMA.read_text())  # second run must not fail


def test_job_defaults(db):
    seed_job(db)
    row = db.execute("SELECT shortlist_target, score_threshold, max_candidates FROM jobs").fetchone()
    assert row == (3, 75, 5)


def test_candidates_unique_job_ploid(db):
    seed_job(db)
    seed_candidate(db, "C1", ploid="P1")
    with pytest.raises(sqlite3.IntegrityError):
        seed_candidate(db, "C2", ploid="P1")
    seed_candidate(db, "C3", ploid="P2")  # different ploid is fine


def test_candidates_requires_existing_job(db):
    with pytest.raises(sqlite3.IntegrityError):
        seed_candidate(db, jid="NOPE")


def test_candidate_job_state_index(db):
    assert (("job_id", "state"), False) in indexes(db, "candidates")


def test_approvals_job_status_index(db):
    assert (("job_id", "status"), False) in indexes(db, "approvals")


def test_events_status_not_before_index(db):
    assert (("status", "not_before"), False) in indexes(db, "events")


def test_event_id_autoincrements_and_defaults(db):
    seed_job(db)
    for _ in range(2):
        db.execute("INSERT INTO events (job_id, type) VALUES ('J1', 'JobCreated')")
    ids = [r[0] for r in db.execute("SELECT id FROM events ORDER BY id")]
    assert ids == [1, 2]
    status, attempts, cand = db.execute("SELECT status, attempts, candidate_id FROM events").fetchone()
    assert (status, attempts, cand) == ("pending", 0, None)


def test_tool_calls_idempotency_key_unique(db):
    seed_job(db)
    seed_candidate(db)
    sql = ("INSERT INTO tool_calls (id, candidate_id, tool, action, idempotency_key, status) "
           "VALUES (?, 'C1', 't', 'a', 'K', 'ok')")
    db.execute(sql, ("T1",))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, ("T2",))


def test_candidate_memory_one_row_per_candidate(db):
    seed_job(db)
    seed_candidate(db)
    db.execute("INSERT INTO candidate_memory (candidate_id) VALUES ('C1')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO candidate_memory (candidate_id) VALUES ('C1')")
    summary = db.execute("SELECT summary FROM candidate_memory").fetchone()[0]
    assert summary == ""


def test_approval_status_defaults_to_pending(db):
    seed_job(db)
    seed_candidate(db)
    db.execute("INSERT INTO approvals (id, job_id, candidate_id, action, payload) "
               "VALUES ('A1', 'J1', 'C1', 'send_outreach', '{}')")
    assert db.execute("SELECT status FROM approvals").fetchone()[0] == "pending"
