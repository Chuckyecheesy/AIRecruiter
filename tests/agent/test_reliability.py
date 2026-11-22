"""Red tests: tool-failure taxonomy gaps (specs/02-tools-and-adapters.md "Error taxonomy",
specs/05-agent-graph.md "Failure handling"). Complements the retry tests in test_graph.py.

Spec rules covered here (not yet implemented in graph.execute):
    transient     1 try + 3 retries (1s, 2s, 4s) then ToolFailed -> escalate
    rate_limited  sleep retry_after_s (cap 30s), up to 3 retries, then ToolFailed -> escalate
    not_found     no retry, result recorded, agent decides next from memory (NOT escalated)
    invalid_input no retry, counts as a decide-error: one re-decide, then escalate
    fatal         no retry, escalate immediately
"""
import pytest

from api.agent import graph
from api.contracts import ActionName as A, EventType as E
from api.db import repo
from api.tools.base import ToolError

from tests.agent.test_graph import (  # noqa: F401  (fixtures + fakes)
    FakePloid, _event, _events, _instant_sleep, db, job, scripted_llm, tools_with,
)


@pytest.fixture
def sleeps(monkeypatch):
    slept = []

    async def fake_sleep(s, *a, **k):
        slept.append(s)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    return slept


async def _run(job, cid, ploid, llm=None):
    llm = llm or scripted_llm(A.enrich_candidate)
    await graph.run_event(_event(job, E.CandidateFound, cid), tools=tools_with(ploid), llm=llm)
    return llm


def _tool_calls(cid):
    with repo.get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM tool_calls WHERE candidate_id=? ORDER BY created_at", (cid,))]


# ---------- transient ----------

async def test_transient_exhausted_is_one_try_plus_three_retries(db, job, sleeps):
    cid = repo.create_candidate(job, "p1", "Ada")
    ploid = FakePloid(enrich_errors=[ToolError("transient")] * 10)
    await _run(job, cid, ploid)
    assert ploid.enrich_calls == 4
    assert sleeps == [1, 2, 4]
    assert repo.get_candidate(cid)["state"] == "NEEDS_HUMAN"


async def test_exhausted_tool_call_row_records_kind_and_attempts(db, job, sleeps):
    cid = repo.create_candidate(job, "p1", "Ada")
    await _run(job, cid, FakePloid(enrich_errors=[ToolError("transient")] * 10))
    (row,) = _tool_calls(cid)
    assert row["status"] == "error" and row["error_kind"] == "transient" and row["attempts"] == 4


async def test_tool_failed_event_carries_kind_attempts_and_action(db, job, sleeps):
    cid = repo.create_candidate(job, "p1", "Ada")
    await _run(job, cid, FakePloid(enrich_errors=[ToolError("transient", "boom")] * 10))
    (ev,) = [e for e in _events(job) if e["type"] == "ToolFailed"]
    assert E.ToolFailed.value == ev["type"]
    import json
    p = json.loads(ev["payload"])
    assert p["kind"] == "transient" and p["attempts"] == 4 and p["action"] == "enrich_candidate"


# ---------- rate_limited ----------

async def test_rate_limited_sleeps_retry_after_when_under_cap(db, job, sleeps):
    cid = repo.create_candidate(job, "p1", "Ada")
    ploid = FakePloid(enrich_errors=[ToolError("rate_limited", retry_after_s=7)])
    await _run(job, cid, ploid)
    assert sleeps == [7]
    assert ploid.enrich_calls == 2
    assert repo.get_candidate(cid)["state"] == "RESEARCHED"


async def test_rate_limited_exhausted_after_three_retries_escalates(db, job, sleeps):
    cid = repo.create_candidate(job, "p1", "Ada")
    ploid = FakePloid(enrich_errors=[ToolError("rate_limited", retry_after_s=600)] * 10)
    await _run(job, cid, ploid)
    assert ploid.enrich_calls == 4
    assert sleeps == [30, 30, 30]
    cand = repo.get_candidate(cid)
    assert cand["state"] == "NEEDS_HUMAN" and cand["prev_state"] == "DISCOVERED"
    assert any(e["type"] == "ToolFailed" for e in _events(job))


# ---------- fatal ----------

async def test_fatal_does_not_sleep_or_retry(db, job, sleeps):
    cid = repo.create_candidate(job, "p1", "Ada")
    ploid = FakePloid(enrich_errors=[ToolError("fatal")] * 5)
    await _run(job, cid, ploid)
    assert ploid.enrich_calls == 1 and sleeps == []
    assert repo.get_candidate(cid)["state"] == "NEEDS_HUMAN"


# ---------- invalid_input: one re-decide, then escalate ----------

async def test_invalid_input_triggers_one_redecide_with_the_error(db, job, sleeps):
    cid = repo.create_candidate(job, "p1", "Ada")
    ploid = FakePloid(enrich_errors=[ToolError("invalid_input", "ploid_id malformed")])
    llm = await _run(job, cid, ploid)
    assert sleeps == []  # never retried in place
    assert len(llm.calls) == 2
    assert "ploid_id malformed" in (llm.calls[1][1] or "")  # the re-decide sees what went wrong
    assert repo.get_candidate(cid)["state"] == "RESEARCHED"  # second attempt succeeded


async def test_invalid_input_twice_escalates(db, job, sleeps):
    cid = repo.create_candidate(job, "p1", "Ada")
    ploid = FakePloid(enrich_errors=[ToolError("invalid_input")] * 5)
    llm = await _run(job, cid, ploid)
    assert len(llm.calls) == 2 and ploid.enrich_calls == 2
    cand = repo.get_candidate(cid)
    assert cand["state"] == "NEEDS_HUMAN" and cand["prev_state"] == "DISCOVERED"
    assert any(e["type"] == "ToolFailed" for e in _events(job))


# ---------- not_found: recorded, NOT escalated ----------

async def test_not_found_is_recorded_and_does_not_escalate(db, job, sleeps):
    cid = repo.create_candidate(job, "p1", "Ada")
    ploid = FakePloid(enrich_errors=[ToolError("not_found", "no Ploid profile p1")] * 5)
    await _run(job, cid, ploid)
    assert ploid.enrich_calls == 1 and sleeps == []
    assert repo.get_candidate(cid)["state"] != "NEEDS_HUMAN"
    assert not any(e["type"] in ("Escalated", "ToolFailed") for e in _events(job))
    (row,) = _tool_calls(cid)
    assert row["status"] == "error" and row["error_kind"] == "not_found"


async def test_not_found_leaves_a_note_in_memory_for_the_next_decision(db, job, sleeps):
    cid = repo.create_candidate(job, "p1", "Ada")
    await _run(job, cid, FakePloid(enrich_errors=[ToolError("not_found", "no Ploid profile p1")]))
    notes = repo.get_memory(cid).research_notes
    assert "no Ploid profile p1" in str(notes)
