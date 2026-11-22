"""Red tests: NEEDS_HUMAN carries the Langfuse trace link (specs/05-agent-graph.md "Observability",
"Failure handling"; checklist 2:50-3:40).

Assumed API in api/observability/langfuse.py:
    RunTrace(trace_id: str | None, url: str | None)
    start_run_trace(*, job_id, candidate_id, state, event_type) -> RunTrace
        One trace per graph run (`recruiter.run`, session_id=job_id). Without
        LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY it is a no-op: RunTrace(None, None).
graph.run_event calls it once per run, stores trace_id/url on agent_runs, and passes the url
to escalate_candidate. Tracing is best-effort: a failing tracer never fails the run.
"""
import json

import pytest

from api.agent import graph
from api.contracts import ActionName as A, EventType as E
from api.db import repo
from api.tools.base import ToolError

from tests.agent.test_graph import (  # noqa: F401  (fixtures + fakes)
    FakePloid, _event, _events, _instant_sleep, db, job, scripted_llm, tools_with,
)

URL = "https://langfuse.test/trace/tr-123"


@pytest.fixture
def fake_tracer(monkeypatch):
    from api.observability import langfuse as lf
    calls = []

    def start(**kw):
        calls.append(kw)
        return lf.RunTrace(trace_id="tr-123", url=URL)

    monkeypatch.setattr(lf, "start_run_trace", start)
    return calls


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)


def _runs():
    with repo.get_conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM agent_runs ORDER BY started_at, id")]


async def _escalating_run(job, errors):
    cid = repo.create_candidate(job, "p1", "Ada")
    await graph.run_event(_event(job, E.CandidateFound, cid),
                          tools=tools_with(FakePloid(enrich_errors=errors)),
                          llm=scripted_llm(A.enrich_candidate))
    return cid


# ---------- retries exhausted -> NEEDS_HUMAN with the trace link ----------

@pytest.mark.parametrize("errors", [
    [ToolError("transient")] * 10,                         # retries exhausted
    [ToolError("rate_limited", retry_after_s=1)] * 10,     # retries exhausted
    [ToolError("fatal")],                                  # immediate
], ids=["transient", "rate_limited", "fatal"])
async def test_needs_human_candidate_has_trace_url(db, job, fake_tracer, errors):
    cid = await _escalating_run(job, errors)
    cand = repo.get_candidate(cid)
    assert cand["state"] == "NEEDS_HUMAN" and cand["prev_state"] == "DISCOVERED"
    assert cand["trace_url"] == URL


async def test_escalated_event_payload_has_trace_url_and_prev_state(db, job, fake_tracer):
    await _escalating_run(job, [ToolError("transient")] * 10)
    (ev,) = [e for e in _events(job) if e["type"] == "Escalated"]
    p = json.loads(ev["payload"])
    assert p["trace_url"] == URL and p["prev_state"] == "DISCOVERED"


async def test_agent_run_row_stores_trace_id_and_url(db, job, fake_tracer):
    await _escalating_run(job, [ToolError("transient")] * 10)
    (run,) = _runs()
    assert run["status"] == "error"
    assert run["langfuse_trace_id"] == "tr-123" and run["trace_url"] == URL


async def test_clean_run_also_stores_trace_ids(db, job, fake_tracer):
    cid = repo.create_candidate(job, "p1", "Ada")
    await graph.run_event(_event(job, E.CandidateFound, cid), tools=tools_with(FakePloid()),
                          llm=scripted_llm(A.enrich_candidate))
    (run,) = _runs()
    assert run["status"] == "ok" and run["langfuse_trace_id"] == "tr-123"
    assert repo.get_candidate(cid)["trace_url"] is None  # only escalations carry the link


async def test_one_trace_per_run_with_session_and_tags_context(db, job, fake_tracer):
    cid = await _escalating_run(job, [ToolError("fatal")])
    assert len(fake_tracer) == 1
    kw = fake_tracer[0]
    assert kw["job_id"] == job and kw["candidate_id"] == cid
    assert kw["event_type"] == "CandidateFound" and kw["state"] == "DISCOVERED"


# ---------- no keys -> no-op, app still runs ----------

@pytest.fixture
def no_keys(monkeypatch):
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST"):
        monkeypatch.delenv(k, raising=False)


def test_start_run_trace_is_a_noop_without_keys(no_keys):
    from api.observability import langfuse as lf
    t = lf.start_run_trace(job_id="J", candidate_id=None, state="job", event_type="Tick")
    assert t.trace_id is None and t.url is None


async def test_escalation_without_keys_has_null_trace_url(db, job, no_keys):
    cid = await _escalating_run(job, [ToolError("transient")] * 10)
    cand = repo.get_candidate(cid)
    assert cand["state"] == "NEEDS_HUMAN" and cand["trace_url"] is None
    (run,) = _runs()
    assert run["langfuse_trace_id"] is None and run["trace_url"] is None


async def test_failing_tracer_never_fails_the_run(db, job, monkeypatch):
    from api.observability import langfuse as lf

    def boom(**kw):
        raise RuntimeError("langfuse down")

    monkeypatch.setattr(lf, "start_run_trace", boom)
    cid = repo.create_candidate(job, "p1", "Ada")
    await graph.run_event(_event(job, E.CandidateFound, cid), tools=tools_with(FakePloid()),
                          llm=scripted_llm(A.enrich_candidate))
    assert repo.get_candidate(cid)["state"] == "RESEARCHED"
