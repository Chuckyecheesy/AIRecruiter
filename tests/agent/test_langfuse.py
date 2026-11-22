"""Red tests: Langfuse tracing content (specs/05-agent-graph.md "Observability";
checklist 2:50-3:40 "one trace per graph run ..., node spans, tool spans, LLM generations",
"Scores: decision_valid, evaluation_score").

Assumed API in api/observability/langfuse.py (a thin layer over the SDK, so tests need no network):
    _backend                       module cache for the backend (None until first use)
    _make_backend() -> Backend     builds the SDK-backed backend; only called when keys are set
    Backend protocol:
        start_trace(*, name, session_id, user_id, tags, metadata) -> (trace_id, url)
        start_observation(*, trace_id, kind, name, input=None, model=None) -> handle
            kind: "span" | "tool" | "generation";  handle.end(*, output=None, error=None)
        score(*, trace_id, name, value, comment=None)
        flush()
    RunTrace(trace_id, url) gains span(name, *, kind="span", input=None, model=None) (a context
    manager yielding a handle with .set_output(x)), score(name, value, comment=None) and flush();
    all are no-ops on the NOOP trace and swallow backend errors.
graph.run_event opens: one span per node run (observe, decide, permission_check, execute,
update_state, escalate), one `tool` span per tool attempt (named by action), one `generation`
per LLM decide call (named "decide"); scores decision_valid (1/0 per decide call) and
evaluation_score (per evaluated candidate); and flushes once at the end of the run.
"""
import itertools

import pytest

from api.agent import graph
from api.contracts import ActionName as A, CandidateState as S, EventType as E
from api.db import repo
from api.tools.base import ToolError

from tests.agent.test_graph import (  # noqa: F401  (fixtures + fakes)
    FakePloid, _event, _instant_sleep, job, scripted_llm, tools_with,
)
from tests.agent import test_lifecycle as lc

URL = "https://langfuse.test/trace/tr-1"


@pytest.fixture
def db(tmp_path):
    repo.init_db(tmp_path / "recruiter.db")
    return repo


class Handle:
    def __init__(self, rec):
        self.rec = rec

    def end(self, *, output=None, error=None):
        assert not self.rec["ended"], f"span {self.rec['name']} ended twice"
        self.rec.update(ended=True, output=output, error=error)


class FakeBackend:
    def __init__(self, fail_observations=False):
        self.traces, self.observations, self.scores, self.flushed = [], [], [], 0
        self.fail_observations = fail_observations
        self._ids = itertools.count(1)

    def start_trace(self, **kw):
        self.traces.append(kw)
        return f"tr-{next(self._ids)}", f"https://langfuse.test/trace/tr-{len(self.traces)}"

    def start_observation(self, *, trace_id, kind, name, input=None, model=None):
        if self.fail_observations:
            raise RuntimeError("langfuse down")
        rec = dict(trace_id=trace_id, kind=kind, name=name, input=input, model=model,
                   output=None, error=None, ended=False)
        self.observations.append(rec)
        return Handle(rec)

    def score(self, *, trace_id, name, value, comment=None):
        self.scores.append(dict(trace_id=trace_id, name=name, value=value, comment=comment))

    def flush(self):
        self.flushed += 1

    def of(self, kind):
        return [o for o in self.observations if o["kind"] == kind]


@pytest.fixture
def backend(monkeypatch):
    from api.observability import langfuse as lf
    fake = FakeBackend()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(lf, "_backend", None, raising=False)
    monkeypatch.setattr(lf, "_make_backend", lambda: fake, raising=False)
    return fake


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)


async def _enrich(job, ploid=None, llm=None):
    cid = repo.create_candidate(job, "p1", "Ada")
    await graph.run_event(_event(job, E.CandidateFound, cid),
                          tools=tools_with(ploid or FakePloid()),
                          llm=llm or scripted_llm(A.enrich_candidate))
    return cid


# ---------- trace context ----------

async def test_one_trace_per_run_with_session_user_and_tags(db, job, backend):
    cid = await _enrich(job)
    (t,) = backend.traces
    assert t["name"] == "recruiter.run"
    assert t["session_id"] == job and t["user_id"] == "ceo"
    assert set(t["tags"]) == {job, cid, "DISCOVERED", "CandidateFound"}


async def test_job_level_run_has_no_candidate_tag(db, job, backend):
    await graph.run_event(_event(job, E.JobCreated), tools=tools_with(FakePloid()),
                          llm=scripted_llm(A.search_candidates))
    (t,) = backend.traces
    assert set(t["tags"]) == {job, "job", "JobCreated"}


def test_no_keys_never_builds_a_backend(monkeypatch):
    from api.observability import langfuse as lf
    for k in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(lf, "_backend", None, raising=False)

    def boom():
        raise AssertionError("backend must not be built without keys")

    monkeypatch.setattr(lf, "_make_backend", boom, raising=False)
    t = lf.start_run_trace(job_id="J", candidate_id=None, state="job", event_type="Tick")
    assert t.trace_id is None and t.url is None
    with t.span("observe") as s:  # NOOP trace: spans, scores and flush are harmless
        s.set_output({"x": 1})
    t.score("decision_valid", 1)
    t.flush()


def test_run_trace_returns_backend_id_and_url(backend):
    from api.observability import langfuse as lf
    t = lf.start_run_trace(job_id="J", candidate_id=None, state="job", event_type="Tick")
    assert t.trace_id == "tr-1" and t.url == URL


# ---------- node spans ----------

async def test_clean_run_has_one_span_per_node_in_order_all_ended(db, job, backend):
    await _enrich(job)
    spans = backend.of("span")
    assert [s["name"] for s in spans] == [
        "observe", "decide", "permission_check", "execute", "update_state"]
    assert all(s["ended"] and s["trace_id"] == "tr-1" for s in spans)


async def test_escalating_run_has_an_escalate_span(db, job, backend):
    await _enrich(job, FakePloid(enrich_errors=[ToolError("fatal")]))
    names = [s["name"] for s in backend.of("span")]
    assert names[-1] == "escalate" and "update_state" not in names
    assert all(s["ended"] for s in backend.observations)


# ---------- tool spans ----------

async def test_tool_span_per_attempt_records_error_kind_and_output(db, job, backend):
    await _enrich(job, FakePloid(enrich_errors=[ToolError("transient")] * 2))
    tools = backend.of("tool")
    assert [t["name"] for t in tools] == ["enrich_candidate"] * 3
    assert [bool(t["error"]) for t in tools] == [True, True, False]
    assert "transient" in str(tools[0]["error"])
    assert tools[2]["output"] is not None and all(t["ended"] for t in tools)


async def test_exhausted_retries_give_four_errored_tool_spans(db, job, backend):
    await _enrich(job, FakePloid(enrich_errors=[ToolError("transient")] * 10))
    tools = backend.of("tool")
    assert len(tools) == 4 and all(t["error"] for t in tools)


# ---------- LLM generations + decision_valid score ----------

async def test_generation_per_decide_call_has_valid_actions_and_chosen_action(db, job, backend):
    await _enrich(job)
    (g,) = backend.of("generation")
    assert g["name"] == "decide" and g["ended"] and g["trace_id"] == "tr-1"
    assert "enrich_candidate" in str(g["input"])
    assert "enrich_candidate" in str(g["output"])


async def test_redecide_makes_two_generations_and_scores_0_then_1(db, job, backend):
    llm = scripted_llm(A.send_outreach, A.enrich_candidate)  # first pick is invalid in DISCOVERED
    await _enrich(job, llm=llm)
    assert len(backend.of("generation")) == 2
    assert [(s["name"], s["value"]) for s in backend.scores if s["name"] == "decision_valid"] == [
        ("decision_valid", 0), ("decision_valid", 1)]


async def test_valid_decision_scores_one_on_the_run_trace(db, job, backend):
    await _enrich(job)
    assert backend.scores == [dict(trace_id="tr-1", name="decision_valid", value=1, comment=None)]


# ---------- evaluation_score ----------

async def test_evaluation_score_is_attached_to_the_trace(db, backend):
    jid = lc.make_job(score_threshold=80)
    cid = lc.make_candidate(jid, S.INTERVIEWED)
    tools = lc.tools_with(evaluator=lc.FakeEvaluator(score=91, recommendation="shortlist"))
    await graph.run_event(lc.event(jid, E.InterviewDone, cid), tools=tools,
                          llm=lc.scripted(A.evaluate_candidate))
    (s,) = [x for x in backend.scores if x["name"] == "evaluation_score"]
    assert s["value"] == 91 and s["trace_id"] == "tr-1"


# ---------- flush + resilience ----------

async def test_flush_once_per_run_even_when_escalating(db, job, backend):
    await _enrich(job, FakePloid(enrich_errors=[ToolError("fatal")]))
    assert backend.flushed == 1


async def test_failing_backend_never_fails_the_run(db, job, backend):
    backend.fail_observations = True
    cid = await _enrich(job)
    assert repo.get_candidate(cid)["state"] == "RESEARCHED"
