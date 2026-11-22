"""Red tests: restart the server mid-run, pending events resume with no state lost
(checklist 2:50-3:40; specs/05-agent-graph.md "Event dispatcher", "Crash recovery").

"Process 1" runs the real graph through a Dispatcher and dies (a BaseException that nothing
catches, like a kill) at a chosen point. "Process 2" is `start_dispatcher` on the same database
and checkpoint file, which runs startup recovery (reset stuck events, release stale locks).

Rules:
    - Every pending / interrupted event is eventually done; none is left 'processing'.
    - A tool call that already succeeded is never re-run on replay (idempotency key).
    - A restricted send that already went out is never sent twice, and its result is not lost:
      the candidate still reaches CONTACTED with its messages row and contact_log entry.
"""
import asyncio

import pytest

from api.agent import graph
from api.agent.dispatcher import Dispatcher, start_dispatcher, stop_dispatcher
from api.contracts import ActionName as A, CandidateState as S, EventType as E
from api.db import repo

from tests.agent.test_graph import FakePloid, scripted_llm, tools_with as ploid_tools
from tests.agent import test_permissions as perm


class Crash(BaseException):
    """The process was killed: not an Exception, so nothing in the app may swallow it."""


@pytest.fixture
def db(tmp_path):
    repo.init_db(tmp_path / "recruiter.db")
    yield repo
    stop_dispatcher()


@pytest.fixture
def job(db):
    from api.contracts import JobState
    jid = db.create_job("Backend Eng", {}, max_candidates=5)
    db.set_job_state(jid, JobState.ACTIVE)
    return jid


def handler_for(tools, llm, owner):
    async def handle(event):
        await graph.run_event(event, tools=tools, llm=llm, owner=owner)
    return handle


async def die_mid_run(job, tools, llm, batch_size=10):
    """Process 1: dispatch until the simulated kill (batch_size=1: it never touches later events)."""
    d = Dispatcher(handler_for(tools, llm, "proc-1"), owner="proc-1", poll_interval=0.01,
                   job_id=job, batch_size=batch_size)
    with pytest.raises(Crash):
        await d.run_once()


def age_everything():
    """Time passes before the restart: claims and locks of the dead process are stale."""
    with repo.get_conn() as c:
        c.execute("UPDATE events SET processed_at='2000-01-01T00:00:00.000Z' WHERE status='processing'")
        c.execute("UPDATE candidates SET locked_at='2000-01-01T00:00:00.000Z' WHERE locked_by IS NOT NULL")


async def restart(job, tools, llm):
    """Process 2: startup recovery, then poll until every event that was open at restart is done.

    Follow-up events the run itself emits are out of scope here (the stub LLM refuses them)."""
    watch = [e["id"] for e in _open_events(job)]
    assert watch, "nothing was waiting for the restart"
    age_everything()
    start_dispatcher(handler_for(tools, llm, "proc-2"), owner="proc-2", poll_interval=0.01,
                     job_id=job, tick_interval=0)
    for _ in range(300):
        if all(_status(i) == "done" for i in watch):
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"events never completed: { {i: _status(i) for i in watch} }")


def _status(eid):
    with repo.get_conn() as c:
        return c.execute("SELECT status FROM events WHERE id=?", (eid,)).fetchone()["status"]


def _open_events(job):
    with repo.get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, type, status FROM events WHERE job_id=? AND status IN ('pending','processing')"
            " AND type NOT IN ('ApprovalRequested','Escalated','ToolFailed','CandidateEnriched')", (job,))]


def enrich_only_llm():
    """Enrich when offered; refuse anything else so follow-up events change nothing."""
    from api.contracts import Decision

    async def llm(valid, error=None):
        if A.enrich_candidate not in valid:
            raise RuntimeError("follow-up decision not under test")
        return Decision(action=A.enrich_candidate, args={}, reasoning="because")

    return llm


def one_shot_crash(monkeypatch, name):
    """Make repo.<name> raise Crash the first time it is called."""
    real, armed = getattr(repo, name), {"on": True}

    def wrapper(*a, **k):
        if armed["on"]:
            armed["on"] = False
            raise Crash(name)
        return real(*a, **k)

    monkeypatch.setattr(repo, name, wrapper)


# ---------- crash while a tool is running ----------

class DiesInEnrich(FakePloid):
    async def enrich(self, ploid_id):
        self.enrich_calls += 1
        raise Crash("killed during enrich")


async def test_pending_event_resumes_after_a_kill_during_a_tool(db, job):
    cid = repo.create_candidate(job, "p1", "Ada")
    repo.emit_event(job, E.CandidateFound, cid)
    llm = enrich_only_llm()
    await die_mid_run(job, ploid_tools(DiesInEnrich()), llm)
    assert repo.get_candidate(cid)["state"] == "DISCOVERED"  # nothing half-applied
    ploid = FakePloid()
    await restart(job, ploid_tools(ploid), llm)
    assert repo.get_candidate(cid)["state"] == "RESEARCHED" and ploid.enrich_calls == 1


async def test_dead_process_lock_does_not_block_the_resumed_event(db, job):
    cid = repo.create_candidate(job, "p1", "Ada")
    repo.emit_event(job, E.CandidateFound, cid)
    assert repo.acquire_lock(cid, "proc-1-dead")  # the lock a killed process never released
    ploid = FakePloid()
    await restart(job, ploid_tools(ploid), enrich_only_llm())
    assert repo.get_candidate(cid)["state"] == "RESEARCHED"
    assert repo.get_candidate(cid)["locked_by"] != "proc-1-dead"  # taken over, not left dangling


# ---------- crash after the tool succeeded, before the state was applied ----------

async def test_completed_tool_call_is_not_re_run_and_its_result_is_not_lost(db, job, monkeypatch):
    cid = repo.create_candidate(job, "p1", "Ada")
    repo.emit_event(job, E.CandidateFound, cid)
    ploid = FakePloid()
    llm = enrich_only_llm()
    one_shot_crash(monkeypatch, "set_candidate_state")  # dies applying the transition
    await die_mid_run(job, ploid_tools(ploid), llm)
    assert ploid.enrich_calls == 1 and repo.get_candidate(cid)["state"] == "DISCOVERED"
    await restart(job, ploid_tools(ploid), llm)
    assert ploid.enrich_calls == 1  # replay used the recorded tool result
    assert repo.get_candidate(cid)["state"] == "RESEARCHED"
    assert repo.get_memory(cid).profile  # the enrichment result reached memory


# ---------- crash after an approved email went out ----------

async def test_sent_email_is_not_resent_and_the_send_is_not_lost(db, job, monkeypatch):
    cid = perm.make_candidate(job)
    aid = repo.create_approval(job, cid, A.send_outreach,
                               {"subject": "Hello Ada", "body": "Approved body"}, "ok")
    repo.decide_approval(aid, True)  # emits ApprovalGranted
    tools = perm.tools_with()
    llm = perm.never_called_llm()
    one_shot_crash(monkeypatch, "set_candidate_state")  # dies after the send, before CONTACTED
    await die_mid_run(job, tools, llm)
    assert len(tools.email.sent) == 1
    await restart(job, tools, llm)
    assert len(tools.email.sent) == 1  # never twice
    assert repo.get_candidate(cid)["state"] == "CONTACTED"
    assert len([m for m in perm.messages_of(cid) if m["direction"] == "out"]) == 1
    assert len([e for e in repo.get_memory(cid).contact_log if e["kind"] == "email_out"]) == 1
    assert repo.get_approval(aid).status == "executed"


# ---------- several candidates, one interrupted ----------

async def test_every_pending_event_is_processed_after_the_restart(db, job):
    cids = [repo.create_candidate(job, f"p{i}", f"Person {i}") for i in range(3)]
    for cid in cids:
        repo.emit_event(job, E.CandidateFound, cid)
    victim = FakePloid()

    async def enrich_then_die(ploid_id):
        victim.enrich_calls += 1
        if victim.enrich_calls == 1:
            raise Crash("killed")
        return await FakePloid.enrich(victim, ploid_id)

    victim.enrich = enrich_then_die
    llm = enrich_only_llm()
    await die_mid_run(job, ploid_tools(victim), llm, batch_size=1)
    assert [repo.get_candidate(c)["state"] for c in cids] == ["DISCOVERED"] * 3
    await restart(job, ploid_tools(FakePloid()), llm)
    assert [repo.get_candidate(c)["state"] for c in cids] == ["RESEARCHED"] * 3
    assert _open_events(job) == []
