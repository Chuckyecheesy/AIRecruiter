"""Red tests for the LangGraph agent core (specs/05-agent-graph.md, lane A checklist 0:20-1:10).

Assumed API in api/agent/graph.py (implement to match):
    thread_id_for(job_id, candidate_id=None) -> str
        "{job_id}:{candidate_id}" or "{job_id}:job"
    async run_event(event, *, tools, llm, checkpoint_path=None) -> AgentState-like dict
        One graph invocation = one event. `tools` is a get_tools()-shaped object,
        `llm` is the decide callable `async (valid_actions, error) -> Decision`.
        Returned dict has: decision, result, error, decide_attempts, run_id.
    build_graph(...).get_graph().nodes contains: observe, decide, permission_check,
        execute, update_state, escalate
Behaviour is taken from spec 05 ("Nodes", "Job-level events") and spec 04 (tool_calls).
"""
import sqlite3
from types import SimpleNamespace

import pytest

from api.contracts import (
    ActionName as A, CandidateState as S, Decision, Event, EventType as E, JobState,
)
from api.db import repo
from api.tools.base import ToolError
from api.tools.models import EnrichedProfile, PloidCandidate

from api.agent import graph


# ---------- fixtures / fakes ----------

@pytest.fixture
def db(tmp_path):
    repo.init_db(tmp_path / "recruiter.db")
    return repo


@pytest.fixture
def job(db):
    jid = db.create_job("Backend Eng", {}, max_candidates=2)
    db.set_job_state(jid, JobState.ACTIVE)
    return jid


def _dec(action, **args):
    return Decision(action=action, args=args, reasoning="because")


def scripted_llm(*actions):
    calls = []

    async def llm(valid, error=None):
        calls.append((list(valid), error))
        return _dec(actions[min(len(calls), len(actions)) - 1])

    llm.calls = calls
    return llm


class FakePloid:
    def __init__(self, found=None, enrich_errors=()):
        self.found = found or []
        self.search_calls = 0
        self.enrich_calls = 0
        self._errors = list(enrich_errors)

    async def search(self, criteria, limit):
        self.search_calls += 1
        return self.found[:limit] if limit else self.found

    async def enrich(self, ploid_id):
        self.enrich_calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return EnrichedProfile(ploid_id=ploid_id, name="Ada", completeness=0.9)


def tools_with(ploid):
    return SimpleNamespace(ploid=ploid, status={})


def _cand(i, name=None, urls=()):
    return PloidCandidate(ploid_id=f"p{i}", name=name or f"Person {i}", profile_urls=list(urls))


def _event(job, type_, candidate_id=None, **payload):
    eid = repo.emit_event(job, type_, candidate_id=candidate_id, payload=payload)
    return Event(id=eid, job_id=job, candidate_id=candidate_id, type=type_, payload=payload)


def _events(job):
    with repo.get_conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM events WHERE job_id=? ORDER BY id", (job,))]


# ---------- structure ----------

def test_thread_id_format():
    assert graph.thread_id_for("J1", "C1") == "J1:C1"
    assert graph.thread_id_for("J1") == "J1:job"


def test_graph_has_spec_nodes():
    names = set(graph.build_graph().get_graph().nodes)
    assert {"observe", "decide", "permission_check", "execute", "update_state", "escalate"} <= names


async def test_checkpoints_use_separate_sqlite_file(db, job, tmp_path):
    ck = tmp_path / "checkpoints.db"
    ploid = FakePloid(found=[_cand(1)])
    await graph.run_event(_event(job, E.JobCreated), tools=tools_with(ploid),
                          llm=scripted_llm(A.search_candidates), checkpoint_path=ck)
    assert ck.exists()
    tables = {r[0] for r in sqlite3.connect(ck).execute("select name from sqlite_master")}
    assert "checkpoints" in tables


# ---------- job-level: JobCreated -> search_candidates ----------

async def test_job_created_only_offers_search_candidates(db, job):
    llm = scripted_llm(A.search_candidates)
    await graph.run_event(_event(job, E.JobCreated), tools=tools_with(FakePloid(found=[_cand(1)])), llm=llm)
    assert llm.calls[0][0] == [A.search_candidates]


async def test_job_created_inserts_discovered_and_emits_candidate_found(db, job):
    ploid = FakePloid(found=[_cand(1), _cand(2)])
    await graph.run_event(_event(job, E.JobCreated), tools=tools_with(ploid),
                          llm=scripted_llm(A.search_candidates))
    with repo.get_conn() as c:
        rows = c.execute("SELECT state FROM candidates WHERE job_id=?", (job,)).fetchall()
    assert [r["state"] for r in rows] == ["DISCOVERED", "DISCOVERED"]
    found = [e for e in _events(job) if e["type"] == "CandidateFound"]
    assert len(found) == 2


async def test_search_respects_max_candidates(db, job):  # job fixture: max_candidates=2
    ploid = FakePloid(found=[_cand(i) for i in range(5)])
    await graph.run_event(_event(job, E.JobCreated), tools=tools_with(ploid),
                          llm=scripted_llm(A.search_candidates))
    with repo.get_conn() as c:
        n = c.execute("SELECT count(*) FROM candidates WHERE job_id=?", (job,)).fetchone()[0]
    assert n == 2


async def test_duplicate_guard_skips_and_emits_no_candidate_found(db, job):
    existing = repo.create_candidate(job, "p1", "Ada Lovelace")
    ploid = FakePloid(found=[
        _cand(1),                                   # same ploid_id
        _cand(9, name="ada  lovelace"),             # same name_key ...
    ])
    ploid.found[1].profile_urls = ["https://github.com/ada"]
    repo.update_memory(existing, profile={"links": {"github": "https://github.com/ada"}})
    await graph.run_event(_event(job, E.JobCreated), tools=tools_with(ploid),
                          llm=scripted_llm(A.search_candidates))
    with repo.get_conn() as c:
        n = c.execute("SELECT count(*) FROM candidates WHERE job_id=?", (job,)).fetchone()[0]
    assert n == 1
    assert [e for e in _events(job) if e["type"] == "CandidateFound"] == []
    assert "duplicate" in repo.get_memory(existing).summary.lower() or any(
        "duplicate" in str(d).lower() for d in repo.get_memory(existing).decisions)


# ---------- candidate-level happy path ----------

async def test_enrich_moves_discovered_to_researched_and_writes_memory(db, job):
    cid = repo.create_candidate(job, "p1", "Ada")
    out = await graph.run_event(_event(job, E.CandidateFound, cid),
                                tools=tools_with(FakePloid()), llm=scripted_llm(A.enrich_candidate))
    assert repo.get_candidate(cid)["state"] == "RESEARCHED"
    mem = repo.get_memory(cid)
    assert mem.profile["name"] == "Ada"
    assert mem.decisions and mem.decisions[-1]["action"] == "enrich_candidate"
    assert mem.summary
    assert out["decision"].action == A.enrich_candidate


async def test_update_state_emits_next_event(db, job):
    cid = repo.create_candidate(job, "p1", "Ada")
    await graph.run_event(_event(job, E.CandidateFound, cid),
                          tools=tools_with(FakePloid()), llm=scripted_llm(A.enrich_candidate))
    nxt = [e for e in _events(job) if e["type"] == "CandidateEnriched" and e["candidate_id"] == cid]
    assert len(nxt) == 1


async def test_execute_writes_tool_calls_row_with_idempotency_key(db, job):
    cid = repo.create_candidate(job, "p1", "Ada")
    await graph.run_event(_event(job, E.CandidateFound, cid),
                          tools=tools_with(FakePloid()), llm=scripted_llm(A.enrich_candidate))
    with repo.get_conn() as c:
        rows = c.execute("SELECT * FROM tool_calls WHERE candidate_id=?", (cid,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["idempotency_key"]
    assert rows[0]["status"] in ("ok", "success", "done")


async def test_agent_run_row_written(db, job):
    cid = repo.create_candidate(job, "p1", "Ada")
    out = await graph.run_event(_event(job, E.CandidateFound, cid),
                                tools=tools_with(FakePloid()), llm=scripted_llm(A.enrich_candidate))
    with repo.get_conn() as c:
        r = c.execute("SELECT * FROM agent_runs WHERE id=?", (out["run_id"],)).fetchone()
    assert r is not None


# ---------- decide validation ----------

async def test_invalid_action_redecides_once_then_succeeds(db, job):
    cid = repo.create_candidate(job, "p1", "Ada")
    llm = scripted_llm(A.shortlist, A.enrich_candidate)  # shortlist invalid in DISCOVERED
    out = await graph.run_event(_event(job, E.CandidateFound, cid),
                                tools=tools_with(FakePloid()), llm=llm)
    assert len(llm.calls) == 2
    assert llm.calls[1][1]  # validation error passed to the second call
    assert out["decide_attempts"] == 2
    assert repo.get_candidate(cid)["state"] == "RESEARCHED"


async def test_two_invalid_actions_escalate_to_needs_human(db, job):
    cid = repo.create_candidate(job, "p1", "Ada")
    ploid = FakePloid()
    await graph.run_event(_event(job, E.CandidateFound, cid),
                          tools=tools_with(ploid), llm=scripted_llm(A.shortlist, A.shortlist))
    cand = repo.get_candidate(cid)
    assert cand["state"] == "NEEDS_HUMAN"
    assert cand["prev_state"] == "DISCOVERED"
    assert ploid.enrich_calls == 0  # nothing executed
    assert any(e["type"] == "Escalated" for e in _events(job))


# ---------- execute: retry taxonomy ----------

async def test_transient_error_is_retried(db, job, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)
    cid = repo.create_candidate(job, "p1", "Ada")
    ploid = FakePloid(enrich_errors=[ToolError("transient"), ToolError("transient")])
    await graph.run_event(_event(job, E.CandidateFound, cid),
                          tools=tools_with(ploid), llm=scripted_llm(A.enrich_candidate))
    assert ploid.enrich_calls == 3
    assert repo.get_candidate(cid)["state"] == "RESEARCHED"


async def test_transient_backoff_is_1_2_4_seconds(db, job, monkeypatch):
    slept = []

    async def fake_sleep(s, *a, **k):
        slept.append(s)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    cid = repo.create_candidate(job, "p1", "Ada")
    ploid = FakePloid(enrich_errors=[ToolError("transient")] * 3)
    await graph.run_event(_event(job, E.CandidateFound, cid),
                          tools=tools_with(ploid), llm=scripted_llm(A.enrich_candidate))
    assert slept[:3] == [1, 2, 4]


# not_found / invalid_input are covered in test_reliability.py
@pytest.mark.parametrize("kind", ["fatal"])
async def test_non_retryable_errors_escalate_immediately(db, job, kind):
    cid = repo.create_candidate(job, "p1", "Ada")
    ploid = FakePloid(enrich_errors=[ToolError(kind)] * 5)
    await graph.run_event(_event(job, E.CandidateFound, cid),
                          tools=tools_with(ploid), llm=scripted_llm(A.enrich_candidate))
    assert ploid.enrich_calls == 1
    assert repo.get_candidate(cid)["state"] == "NEEDS_HUMAN"
    assert any(e["type"] == "ToolFailed" for e in _events(job))


async def test_retries_exhausted_escalates_with_prev_state(db, job, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)
    cid = repo.create_candidate(job, "p1", "Ada")
    ploid = FakePloid(enrich_errors=[ToolError("transient")] * 10)
    await graph.run_event(_event(job, E.CandidateFound, cid),
                          tools=tools_with(ploid), llm=scripted_llm(A.enrich_candidate))
    cand = repo.get_candidate(cid)
    assert cand["state"] == "NEEDS_HUMAN"
    assert cand["prev_state"] == "DISCOVERED"
    assert any(e["type"] == "Escalated" for e in _events(job))


async def test_rate_limited_sleep_is_capped_at_30s(db, job, monkeypatch):
    slept = []

    async def fake_sleep(s, *a, **k):
        slept.append(s)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    cid = repo.create_candidate(job, "p1", "Ada")
    ploid = FakePloid(enrich_errors=[ToolError("rate_limited", retry_after_s=600)])
    await graph.run_event(_event(job, E.CandidateFound, cid),
                          tools=tools_with(ploid), llm=scripted_llm(A.enrich_candidate))
    assert slept and max(slept) <= 30


# ---------- lock ----------

async def test_lock_released_after_run(db, job):
    cid = repo.create_candidate(job, "p1", "Ada")
    await graph.run_event(_event(job, E.CandidateFound, cid),
                          tools=tools_with(FakePloid()), llm=scripted_llm(A.enrich_candidate))
    assert repo.get_candidate(cid)["locked_by"] is None


async def test_lock_released_even_when_escalated(db, job):
    cid = repo.create_candidate(job, "p1", "Ada")
    await graph.run_event(_event(job, E.CandidateFound, cid),
                          tools=tools_with(FakePloid(enrich_errors=[ToolError("fatal")])),
                          llm=scripted_llm(A.enrich_candidate))
    assert repo.get_candidate(cid)["locked_by"] is None


async def _instant_sleep(*a, **k):
    return None


async def test_state_does_not_leak_between_runs_on_the_same_thread(db, job, monkeypatch):
    """The thread id is per candidate, so LangGraph restores the last checkpoint each run."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async def keep(self, thread_id):  # disable pruning so the old checkpoint is really restored
        return None

    monkeypatch.setattr(AsyncSqliteSaver, "adelete_thread", keep)
    cid = repo.create_candidate(job, "p1", "Ada")
    tools = tools_with(FakePloid(enrich_errors=[ToolError("fatal")]))
    first = await graph.run_event(_event(job, E.CandidateFound, cid), tools=tools,
                                  llm=scripted_llm(A.enrich_candidate))
    assert first["error"]
    # NEEDS_HUMAN + a non-HumanResolved event => observe skips; nothing may carry over.
    second = await graph.run_event(_event(job, E.Tick, cid), tools=tools,
                                   llm=scripted_llm(A.enrich_candidate))
    assert second["decision"] is None and second["error"] is None
    with repo.get_conn() as c:
        st = [r["status"] for r in c.execute("SELECT status FROM agent_runs ORDER BY started_at, id")]
    assert st == ["error", "ok"]


async def test_checkpoints_pruned_after_clean_run(db, job, tmp_path):
    ck = tmp_path / "ck.db"
    cid = repo.create_candidate(job, "p1", "Ada")
    for _ in range(2):
        await graph.run_event(_event(job, E.CandidateFound, cid), tools=tools_with(FakePloid()),
                              llm=scripted_llm(A.enrich_candidate), checkpoint_path=ck)
    n = sqlite3.connect(ck).execute("select count(*) from checkpoints").fetchone()[0]
    assert n == 0


async def test_crashed_run_keeps_its_checkpoint(db, job, tmp_path):
    ck = tmp_path / "ck.db"
    cid = repo.create_candidate(job, "p1", "Ada")

    class Boom(FakePloid):
        async def enrich(self, ploid_id):
            raise RuntimeError("process died")

    with pytest.raises(RuntimeError):
        await graph.run_event(_event(job, E.CandidateFound, cid), tools=tools_with(Boom()),
                              llm=scripted_llm(A.enrich_candidate), checkpoint_path=ck)
    assert sqlite3.connect(ck).execute("select count(*) from checkpoints").fetchone()[0] > 0
    assert repo.get_candidate(cid)["locked_by"] is None  # lock still released


async def test_run_refuses_a_candidate_locked_by_someone_else(db, job):
    cid = repo.create_candidate(job, "p1", "Ada")
    assert repo.acquire_lock(cid, "other-worker")
    with pytest.raises(repo.LockBusy):
        await graph.run_event(_event(job, E.CandidateFound, cid), tools=tools_with(FakePloid()),
                              llm=scripted_llm(A.enrich_candidate))
    assert repo.get_candidate(cid)["state"] == "DISCOVERED"
    assert repo.get_candidate(cid)["locked_by"] == "other-worker"


# ---------- restricted actions park as one pending approval, with no wake-up loop ----------

async def _drafted_candidate(job):
    cid = repo.create_candidate(job, "p1", "Ada", state=S.OUTREACH_DRAFTED)
    return cid


def _approvals(cid):
    with repo.get_conn() as c:
        return c.execute("SELECT * FROM approvals WHERE candidate_id=?", (cid,)).fetchall()


async def test_restricted_action_creates_one_pending_approval_and_stops(db, job):
    cid = await _drafted_candidate(job)
    await graph.run_event(_event(job, E.OutreachDrafted, cid), tools=tools_with(FakePloid()),
                          llm=scripted_llm(A.send_outreach))
    assert [a["status"] for a in _approvals(cid)] == ["pending"]
    assert repo.get_candidate(cid)["state"] == "OUTREACH_DRAFTED"
    assert len([e for e in _events(job) if e["type"] == "ApprovalRequested"]) == 1


async def test_approval_requested_event_does_not_wake_the_agent(db, job):
    cid = await _drafted_candidate(job)
    llm = scripted_llm(A.send_outreach)
    await graph.run_event(_event(job, E.ApprovalRequested, cid), tools=tools_with(FakePloid()), llm=llm)
    assert llm.calls == [] and _approvals(cid) == []


async def test_same_pending_approval_is_not_duplicated(db, job):
    cid = await _drafted_candidate(job)
    for _ in range(3):
        await graph.run_event(_event(job, E.OutreachDrafted, cid), tools=tools_with(FakePloid()),
                              llm=scripted_llm(A.send_outreach))
    assert len(_approvals(cid)) == 1
    assert len([e for e in _events(job) if e["type"] == "ApprovalRequested"]) == 1


@pytest.mark.parametrize("etype", [E.Escalated, E.ToolFailed])
async def test_notification_events_do_not_wake_the_agent(db, job, etype):
    cid = repo.create_candidate(job, "p1", "Ada")
    llm = scripted_llm(A.enrich_candidate)
    await graph.run_event(_event(job, etype, cid), tools=tools_with(FakePloid()), llm=llm)
    assert llm.calls == []
