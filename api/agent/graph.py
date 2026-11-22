"""LangGraph recruiter agent (specs/05-agent-graph.md).

One invocation = one event for one candidate:
    observe -> decide -> permission_check -> execute -> update_state -> END
with `escalate` reachable from decide/permission_check/execute.
Only code applies transitions; the LLM only picks from `valid_actions`.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from ulid import ULID

from api.agent.decide import decide_validated
from api.agent.permissions import permission_class
from api.agent.state_machine import apply_transition, job_valid_actions, valid_actions
from api.contracts import (
    ActionName as A, CandidateMemory, CandidateState as S, Decision, Event, EventType as E,
    PermissionClass,
)
from api.db import repo
from api.tools.base import ToolError
from api.tools.models import SearchCriteria

DEFAULT_CHECKPOINT_DB = "data/checkpoints.db"
MAX_RETRIES = 3
TRANSIENT_BACKOFF_S = (1, 2, 4)
RATE_LIMIT_CAP_S = 30


_SERDE = JsonPlusSerializer(allowed_msgpack_modules=[
    ("api.contracts", n) for n in ("Event", "EventType", "CandidateMemory", "ActionName", "Decision")])


class AgentState(TypedDict, total=False):
    job_id: str
    candidate_id: Optional[str]
    event: Event
    job: dict
    candidate: Optional[dict]
    memory: Optional[CandidateMemory]
    valid_actions: list[A]
    decision: Optional[Decision]
    approval: Optional[Any]
    result: Optional[dict]
    error: Optional[dict]
    decide_attempts: int
    run_id: str
    route: str          # observe/decide/permission_check/execute decide the next hop
    escalate_reason: str


def thread_id_for(job_id: str, candidate_id: Optional[str] = None) -> str:
    return f"{job_id}:{candidate_id}" if candidate_id else f"{job_id}:job"


# ---------- action executors (adapters in, plain dicts out; never touch the DB) ----------

def _urls(profile: dict) -> list[str]:
    return [str(u) for u in list((profile.get("links") or {}).values()) + list(profile.get("profile_urls") or [])]


async def _search(tools, st) -> dict:
    job = st["job"]
    form = job.get("form") or {}
    criteria = SearchCriteria(
        title=job["title"],
        must_have_skills=list(form.get("must_have_skills") or form.get("must_haves") or []),
        nice_to_have_skills=list(form.get("nice_to_have_skills") or form.get("nice_to_haves") or []),
        location=form.get("location"), seniority=form.get("seniority"),
        free_text=form.get("free_text") or form.get("description"))
    found = await tools.ploid.search(criteria, job["max_candidates"])
    return {"candidates": [c.model_dump(mode="json") for c in found]}


async def _enrich(tools, st) -> dict:
    prof = await tools.ploid.enrich(st["candidate"]["ploid_id"])
    return {"profile": prof.model_dump(mode="json")}


async def _browser_research(tools, st) -> dict:
    mem = st["memory"]
    urls = st["decision"].args.get("urls") or _urls(mem.profile)
    qs = st["decision"].args.get("questions") or [f"Evidence of: {s}" for s in
                                                  (st["job"].get("form") or {}).get("must_haves", [])]
    found = await tools.browser.research(list(urls), list(qs))
    return {"research": found.model_dump(mode="json")}


async def _find_contact(tools, st) -> dict:
    mem = st["memory"]
    links = {k: str(v) for k, v in (mem.profile.get("links") or {}).items()}
    hint = ((mem.profile.get("experience") or [{}])[0] or {}).get("company")
    info = await tools.fiber.find_contact(st["candidate"]["name"], links, hint)
    return {"contact": info.model_dump(mode="json")}


async def _draft(tools, st) -> dict:
    fb = st["decision"].args.get("feedback") or st["event"].payload.get("feedback")
    d = await tools.drafter.outreach(SimpleNamespace(**st["job"]), st["memory"], fb)
    return {"draft": d.model_dump(mode="json")}


async def _wait(tools, st) -> dict:
    return {}


EXECUTORS = {
    A.search_candidates: ("ploid", _search),
    A.enrich_candidate: ("ploid", _enrich),
    A.browser_research: ("browser", _browser_research),
    A.find_contact: ("fiber", _find_contact),
    A.draft_outreach: ("drafter", _draft),
    A.redraft_outreach: ("drafter", _draft),
    A.wait: ("none", _wait),
}

NEXT_EVENT = {
    A.enrich_candidate: E.CandidateEnriched,
    A.browser_research: E.CandidateEnriched,
    A.find_contact: E.ContactFound,
    A.draft_outreach: E.OutreachDrafted,
    A.redraft_outreach: E.OutreachDrafted,
}


def _memory_patch(action: A, result: dict, mem: CandidateMemory) -> dict:
    if action == A.enrich_candidate:
        return {"profile": {**mem.profile, **result["profile"]}}
    if action == A.browser_research:
        return {"research_notes": {**mem.research_notes, **result["research"]}}
    if action == A.find_contact:
        return {"contact": result["contact"]}
    if action in (A.draft_outreach, A.redraft_outreach):
        return {"contact_log": mem.contact_log + [{"kind": "draft", "ts": repo.now_iso(), **result["draft"]}]}
    return {}


# ---------- graph ----------

class _Deps:
    def __init__(self, tools: Any, llm: Any) -> None:
        self.tools, self.llm = tools, llm


def build_graph(tools: Any = None, llm: Any = None, checkpointer: Any = None):
    deps = _Deps(tools, llm)

    async def observe(st: AgentState) -> dict:
        ev = st["event"]
        job = repo.get_job(st["job_id"])
        out: dict = {"job": job}
        cid = st.get("candidate_id")
        if cid is None:
            out["valid_actions"] = job_valid_actions(job) if ev.type == E.JobCreated else []
            out["route"] = "decide" if out["valid_actions"] else "end"
            return out
        cand = repo.get_candidate(cid)
        state = S(cand["state"])
        out.update(candidate=cand, memory=repo.get_memory(cid))
        # Escalations/failures already put the candidate in front of a human; don't loop on them.
        if state == S.NEEDS_HUMAN and ev.type != E.HumanResolved:
            out["route"] = "end"
        else:
            out["valid_actions"] = valid_actions(state, job)
            out["route"] = "decide" if out["valid_actions"] else "end"
        return out

    async def decide(st: AgentState) -> dict:
        ctx = SimpleNamespace(
            job=SimpleNamespace(**st["job"]),
            candidate=SimpleNamespace(**st["candidate"]) if st.get("candidate") else None,
            memory=st.get("memory"), event=st["event"])
        llm = deps.llm
        if llm is None:
            from api.agent.llm import make_llm
            from api.config import settings
            llm = make_llm(settings.llm_mode, ctx)
        state = S(st["candidate"]["state"]) if st.get("candidate") else "job"
        outcome = await decide_validated(llm, state, st["job"], valid=st["valid_actions"])
        if outcome.escalate:
            return {"decide_attempts": outcome.attempts, "route": "escalate",
                    "escalate_reason": f"invalid decision twice: {outcome.error}"}
        d = outcome.decision
        if d is None:
            return {"decide_attempts": outcome.attempts, "route": "end"}
        return {"decision": d, "decide_attempts": outcome.attempts,
                "route": "escalate" if d.action == A.escalate else "permission_check",
                "escalate_reason": d.reasoning}

    async def permission_check(st: AgentState) -> dict:
        d = st["decision"]
        if permission_class(d.action) != PermissionClass.RESTRICTED:
            return {"route": "execute"}
        # Approval policy (auto-approve, execute-from-stored-payload) is the next slice:
        # for now a restricted action always parks as a pending approval and ends the run.
        aid = repo.create_approval(st["job_id"], st["candidate_id"], d.action, d.args, d.reasoning)
        repo.emit_event(st["job_id"], E.ApprovalRequested, st["candidate_id"],
                        {"approval_id": aid, "action": d.action.value})
        return {"route": "end"}

    async def execute(st: AgentState) -> dict:
        d: Decision = st["decision"]
        if d.action not in EXECUTORS:
            return _fail(st, ToolError("fatal", f"no executor for {d.action.value}"), 1)
        tool, fn = EXECUTORS[d.action]
        key = f"{st['event'].id}:{d.action.value}:{st.get('candidate_id') or st['job_id']}"
        prior = repo.get_tool_call(key)
        if prior and prior["status"] == "ok":  # event replayed after a crash: don't re-run
            return {"result": prior["output"], "route": "update_state"}
        tools = deps.tools
        if tools is None:
            from api.tools import get_tools
            tools = deps.tools = get_tools()
        started, attempt = time.monotonic(), 0
        while True:
            attempt += 1
            try:
                result = await fn(tools, st)
            except ToolError as e:
                delay = _retry_delay(e, attempt)
                if delay is None:
                    repo.record_tool_call(
                        run_id=st["run_id"], candidate_id=st.get("candidate_id"), tool=tool,
                        action=d.action, idempotency_key=key, input=d.args, output=None,
                        status="error", error_kind=e.kind, attempts=attempt)
                    return _fail(st, e, attempt)
                await asyncio.sleep(delay)
                continue
            repo.record_tool_call(
                run_id=st["run_id"], candidate_id=st.get("candidate_id"), tool=tool, action=d.action,
                idempotency_key=key, input=d.args, output=result, status="ok", attempts=attempt,
                latency_ms=int((time.monotonic() - started) * 1000))
            return {"result": result, "route": "update_state"}

    def _fail(st: AgentState, e: ToolError, attempts: int) -> dict:
        repo.emit_event(st["job_id"], E.ToolFailed, st.get("candidate_id"),
                        {"action": st["decision"].action.value, "kind": e.kind, "message": str(e),
                         "attempts": attempts})
        return {"error": {"kind": e.kind, "message": str(e)}, "route": "escalate",
                "escalate_reason": f"{st['decision'].action.value} failed ({e.kind}): {e}"}

    async def update_state(st: AgentState) -> dict:
        d, result, job = st["decision"], st["result"] or {}, st["job"]
        cid = st.get("candidate_id")
        if cid is None:
            _store_search_results(job, result)
            return {}
        cand, mem = st["candidate"], st["memory"]
        new_state = apply_transition(S(cand["state"]), d.action, result)
        patch = _memory_patch(d.action, result, mem)
        decisions = mem.decisions + [{
            "ts": repo.now_iso(), "action": d.action.value, "reasoning": d.reasoning,
            "outcome": f"{cand['state']} -> {new_state.value}"}]
        summary = (mem.summary + f" {d.action.value}: {d.reasoning}")[-800:].strip()
        repo.set_candidate_state(cid, new_state)
        repo.update_memory(cid, **patch, decisions=decisions, summary=summary)
        if d.action in NEXT_EVENT:
            repo.emit_event(st["job_id"], NEXT_EVENT[d.action], cid, {"from": cand["state"]})
        return {}

    async def escalate(st: AgentState) -> dict:
        reason = st.get("escalate_reason") or "escalated"
        if st.get("candidate_id"):
            repo.escalate_candidate(st["candidate_id"], reason)
        else:
            repo.emit_event(st["job_id"], E.Escalated, None, {"reason": reason})
        return {}

    g = StateGraph(AgentState)
    for name, fn in [("observe", observe), ("decide", decide), ("permission_check", permission_check),
                     ("execute", execute), ("update_state", update_state), ("escalate", escalate)]:
        g.add_node(name, fn)
    g.add_edge(START, "observe")
    for src, targets in {
        "observe": {"decide"}, "decide": {"permission_check", "escalate"},
        "permission_check": {"execute"}, "execute": {"update_state", "escalate"},
    }.items():
        g.add_conditional_edges(
            src, lambda s, ok=targets: s["route"] if s["route"] in ok else END,
            {**{t: t for t in targets}, END: END})
    g.add_edge("update_state", END)
    g.add_edge("escalate", END)
    return g.compile(checkpointer=checkpointer)


def _retry_delay(e: ToolError, attempt: int) -> Optional[float]:
    """Seconds to wait before retrying, or None when the error is not retryable / exhausted."""
    if attempt > MAX_RETRIES:
        return None
    if e.kind == "transient":
        return TRANSIENT_BACKOFF_S[attempt - 1]
    if e.kind == "rate_limited":
        return min(e.retry_after_s if e.retry_after_s is not None else 5, RATE_LIMIT_CAP_S)
    return None  # not_found / invalid_input / fatal


def _store_search_results(job: dict, result: dict) -> None:
    """Duplicate guard + insert DISCOVERED candidates up to max_candidates, emit CandidateFound."""
    room = job["max_candidates"] - repo.count_candidates(job["id"])
    for c in result.get("candidates", []):
        urls = c.get("profile_urls") or []
        dup = repo.find_duplicate_candidate(job["id"], c["ploid_id"], c["name"], urls)
        if dup:
            repo.append_decision(dup, {
                "ts": repo.now_iso(), "action": "search_candidates",
                "reasoning": f"duplicate found, skipped: {c['name']} ({c['ploid_id']})",
                "outcome": "skipped"})
            continue
        if room <= 0:
            break
        cid = repo.create_candidate(job["id"], c["ploid_id"], c["name"])
        repo.update_memory(cid, profile={k: c.get(k) for k in ("headline", "location", "profile_urls")})
        repo.emit_event(job["id"], E.CandidateFound, cid, {"ploid_id": c["ploid_id"]})
        room -= 1


# ---------- entrypoint ----------

async def run_event(
    event: Event, *, tools: Any = None, llm: Any = None,
    checkpoint_path: Optional[str | Path] = None, owner: Optional[str] = None,
) -> dict:
    """Run the graph once for `event`. `owner` = lock owner already held by the dispatcher."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    path = Path(checkpoint_path or os.environ.get("CHECKPOINT_DB") or DEFAULT_CHECKPOINT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    cid = event.candidate_id
    run_id = repo.start_run(event.job_id, cid, event.id)
    lock = (repo.candidate_lock(cid, f"run-{ULID()}") if cid and owner is None
            else contextlib.nullcontext())
    status, err, final = "ok", None, {}
    try:
        with lock:
            async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
                saver.serde = _SERDE
                app = build_graph(tools, llm, saver)
                final = await app.ainvoke(
                    # The thread is reused for every event of a candidate and LangGraph restores the
                    # last checkpoint, so every per-run field must be reset explicitly.
                    {"job_id": event.job_id, "candidate_id": cid, "event": event,
                     "decide_attempts": 0, "run_id": run_id, "route": "decide",
                     "decision": None, "result": None, "error": None, "approval": None,
                     "escalate_reason": "", "valid_actions": []},
                    config={"configurable": {"thread_id": thread_id_for(event.job_id, cid)}})
        if final.get("error") or final.get("route") == "escalate":
            status = "error"
        return {**final, "run_id": run_id}
    except BaseException as e:  # noqa: BLE001 - recorded, then re-raised for the dispatcher
        status, err = "error", repr(e)
        raise
    finally:
        d = final.get("decision")
        repo.finish_run(run_id, status, d.action.value if d else None, err)


async def handle_event(event: Event, owner: Optional[str] = None) -> None:
    """Dispatcher handler (default tools and LLM from config)."""
    await run_event(event, owner=owner)
