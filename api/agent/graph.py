"""LangGraph recruiter agent (specs/05-agent-graph.md).

One invocation = one event for one candidate:
    observe -> decide -> permission_check -> execute -> update_state -> END
with `escalate` reachable from decide/permission_check/execute.
Only code applies transitions; the LLM only picks from `valid_actions`.
"""
from __future__ import annotations

import asyncio
import logging
import inspect
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from ulid import ULID

from api.agent.decide import decide_validated
from api.agent.permissions import (
    PermissionDenied, Verdict, assert_approved, check_permission, execute_approved,
)
from api.agent.state_machine import apply_transition, job_valid_actions, valid_actions
from api.contracts import (
    ActionName as A, CandidateMemory, CandidateState as S, Decision, Event, EventType as E,
    JobState, PermissionClass,
)
from api.db import repo
from api.observability import langfuse as tracing
from api.tools.base import ToolError
from api.config import settings
from api.tools.models import Draft, Message, SearchCriteria
from api.tools.persona import pick_disposition

log = logging.getLogger(__name__)
DEFAULT_CHECKPOINT_DB = "data/checkpoints.db"
MAX_RETRIES = 3
TRANSIENT_BACKOFF_S = (1, 2, 4)
RATE_LIMIT_CAP_S = 30
# not_found is normally recorded and the agent re-decides from memory. These actions have no
# alternative next step (the event is the only trigger), so a not_found there needs a human.
NOT_FOUND_ESCALATES = frozenset({A.process_interview})


_SERDE = JsonPlusSerializer(allowed_msgpack_modules=[
    ("api.contracts", n) for n in ("Event", "EventType", "CandidateMemory", "ActionName", "Decision")])


# Emitted for the dashboard/log only. Waking the agent on them would loop
# (e.g. ApprovalRequested -> decide send_outreach -> ApprovalRequested ...).
NOTIFICATION_EVENTS = frozenset({E.ApprovalRequested, E.Escalated, E.ToolFailed})


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
    escalate_extra: dict
    tool_redecided: bool    # an invalid_input tool error already used the one re-decide
    redecide_error: Optional[str]
    trace_url: Optional[str]


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
    prev = _latest_draft(st["memory"]) if fb else None
    previous = Draft(subject=prev.get("subject"), body=prev.get("body") or "") if prev and prev.get("body") else None
    kw = {"previous": previous} if previous is not None and "previous" in inspect.signature(
        tools.drafter.outreach).parameters else {}
    d = await tools.drafter.outreach(SimpleNamespace(**st["job"]), st["memory"], fb, **kw)
    return {"draft": d.model_dump(mode="json")}


async def _wait(tools, st) -> dict:
    return {}


def _thread(mem: CandidateMemory, extra_in: Optional[str] = None) -> list[Message]:
    """Email thread so far (oldest first), rebuilt from the contact log."""
    msgs = [Message(direction="out" if e["kind"] == "email_out" else "in",
                    subject=e.get("subject"), body=e.get("body", ""))
            for e in mem.contact_log if e.get("kind") in ("email_out", "email_in")]
    if extra_in is not None:
        msgs.append(Message(direction="in", body=extra_in))
    return msgs


def _latest_draft(mem: CandidateMemory) -> Optional[dict]:
    for e in reversed(mem.contact_log or []):
        if isinstance(e, dict) and e.get("kind") == "draft":
            return e
    return None


def _role_blurb(job: dict) -> str:
    """Short, candidate-safe context for the voice interview (the join link is public: no rubric
    weights, no agent notes)."""
    form = job.get("form") or {}
    if isinstance(form, str):
        try:
            form = json.loads(form)
        except ValueError:
            form = {}
    musts = ", ".join((form or {}).get("must_haves") or [])
    return f"{job.get('title') or 'Open'} role." + (f" Looking for: {musts}." if musts else "")


def _interview_questions(job: dict) -> list[str]:
    rubric = job.get("rubric") or {}
    if isinstance(rubric, str):
        try:
            rubric = json.loads(rubric)
        except ValueError:
            rubric = {}
    themes = [t for t in (rubric or {}).get("interview_themes") or [] if t][:4]
    return themes or ["Walk me through a recent project you're proud of.",
                      "How did you measure whether it worked?",
                      "What would you do differently next time?"]


def _with_default_args(st, d: Decision) -> Decision:
    """Fill args code can derive from memory, so the approval payload is complete even when the
    decide LLM (or the stub) returns minimal args (spec 05: args are derived by code)."""
    args = dict(d.args or {})
    mem, job = st.get("memory"), st.get("job") or {}
    if d.action in (A.send_outreach, A.send_followup) and not args.get("body"):
        draft = _latest_draft(mem) if mem is not None else None
        if draft:
            args.setdefault("subject", draft.get("subject"))
            args["body"] = draft.get("body") or ""
        elif d.action == A.send_followup:
            args.setdefault("subject", f"Re: {job.get('title') or 'our role'}")
            args["body"] = ("Just bumping this in case it got buried. Would a quick 15-minute call "
                            "this week work?\n\nBest,\nthe CEO")
    if d.action == A.schedule_interview:
        if not args.get("questions"):
            args["questions"] = _interview_questions(job)
        if not args.get("context"):
            args["context"] = _role_blurb(job)
    return d if args == (d.args or {}) else d.model_copy(update={"args": args})


def _thread_id(st) -> str:
    return f"thread-{st['candidate_id']}"


async def _send_email(tools, st) -> dict:
    d, mem = st["decision"], st["memory"]
    to = (mem.contact or {}).get("email")
    if not to:
        raise ToolError("invalid_input", "no contact email on file")
    subject, body = d.args.get("subject"), d.args.get("body") or ""
    tid = _thread_id(st)
    receipt = await tools.email.send(to, subject or "", body, tid)
    out = {"to": to, "subject": subject, "body": body, "thread_id": tid,
           "message_id": getattr(receipt, "message_id", None)}
    if d.action == A.send_outreach:
        # The simulated candidate decides whether and how to answer (pinned for scripted demos).
        disposition = (st["candidate"] or {}).get("demo_disposition") or pick_disposition()
        out["disposition"] = disposition
        if disposition != "no_reply":
            sent = Message(direction="out", subject=subject, body=body)
            out["persona_reply"] = await tools.persona.reply(
                mem.profile, _thread(mem) + [sent], disposition)
    return out


async def _schedule_interview(tools, st) -> dict:
    d, mem = st["decision"], st["memory"]
    session = await tools.elevenlabs.create_interview(
        st["candidate_id"], list(d.args.get("questions") or []),
        d.args.get("context") or _role_blurb(st["job"] or {}),
        job_id=st["job_id"], candidate_name=(st["candidate"] or {}).get("name"))
    expires = getattr(session, "expires_at", None)
    invite = await _email_interview_link(tools, st, getattr(session, "join_url", None))
    return {"session": {"conversation_id": getattr(session, "conversation_id", None),
                        "join_url": getattr(session, "join_url", None),
                        "expires_at": expires.isoformat() if hasattr(expires, "isoformat") else expires},
            "questions": list(d.args.get("questions") or []), "invite": invite}


async def _email_interview_link(tools, st, join_url: Optional[str]) -> Optional[dict]:
    """Email the candidate the voice-interview link (real only for allowlisted demo inboxes; see
    api/tools/email_sim.py). Never blocks scheduling: the link is also shown in the dashboard."""
    mem, cand, job = st["memory"], st["candidate"] or {}, st["job"] or {}
    to = (mem.contact or {}).get("email") if mem is not None else None
    if not (to and join_url):
        return None
    first = (cand.get("name") or "there").split()[0]
    body = (f"Hi {first},\n\nThanks for your interest in the {job.get('title') or 'open'} role! "
            f"Here's the link to a short (~5 min) voice screen with our recruiter agent. Open it in "
            f"Chrome and allow microphone access:\n\n{join_url}\n\nThe link works for the next 2 hours.\n\n"
            f"Best,\nthe CEO")
    try:
        r = await tools.email.send(to, f"Your voice interview: {job.get('title') or 'next step'}", body,
                                   _thread_id(st))
        return {"to": to, "message_id": getattr(r, "message_id", None), "simulated": getattr(r, "simulated", True)}
    except Exception as e:  # noqa: BLE001 - the invite is best-effort
        log.warning("interview invite email failed for %s: %r", st["candidate_id"], e)
        return {"to": to, "error": str(e)}


async def _process_reply(tools, st) -> dict:
    text = st["event"].payload.get("body") or ""
    cls = await tools.classifier.classify(text)
    out = {"classification": cls.intent, "body": text, "reasoning": cls.reasoning}
    if cls.intent == "question":
        draft = await tools.drafter.reply(SimpleNamespace(**st["job"]), st["memory"],
                                          _thread(st["memory"], extra_in=text))
        out["draft"] = draft.model_dump(mode="json")
    return out


async def _process_interview(tools, st) -> dict:
    payload = st["event"].payload
    tr = payload.get("transcript")
    if tr is None:  # poller path: fetch it from ElevenLabs
        conv = payload.get("conversation_id")
        if not conv:
            raise ToolError("not_found", "InterviewDone carries neither a transcript nor a conversation id")
        tr = await tools.elevenlabs.get_transcript(conv)
        if tr is None:
            raise ToolError("not_found", f"no transcript available yet for conversation {conv}")
    if hasattr(tr, "model_dump"):
        tr = tr.model_dump(mode="json")
    return {"transcript": tr}


async def _evaluate(tools, st) -> dict:
    job = st["job"]
    ev = await tools.evaluator.evaluate(SimpleNamespace(**job), st["memory"], job["score_threshold"])
    return {"evaluation": ev.model_dump(mode="json")}


async def _resume(tools, st) -> dict:
    return {"prev_state": (st["candidate"] or {}).get("prev_state")}


EXECUTORS = {
    A.search_candidates: ("ploid", _search),
    A.enrich_candidate: ("ploid", _enrich),
    A.browser_research: ("browser", _browser_research),
    A.find_contact: ("fiber", _find_contact),
    A.draft_outreach: ("drafter", _draft),
    A.redraft_outreach: ("drafter", _draft),
    A.wait: ("none", _wait),
    A.send_outreach: ("email", _send_email),
    A.send_followup: ("email", _send_email),
    A.schedule_interview: ("elevenlabs", _schedule_interview),
    A.reject_candidate_proposal: ("none", _wait),
    A.process_reply: ("classifier", _process_reply),
    A.process_interview: ("elevenlabs", _process_interview),
    A.evaluate_candidate: ("evaluator", _evaluate),
    A.mark_stale: ("none", _wait),
    A.shortlist: ("none", _wait),
    A.reactivate: ("none", _wait),
    A.resume: ("none", _resume),
}

NEXT_EVENT = {
    A.enrich_candidate: E.CandidateEnriched,
    A.browser_research: E.CandidateEnriched,
    A.find_contact: E.ContactFound,
    A.draft_outreach: E.OutreachDrafted,
    A.redraft_outreach: E.OutreachDrafted,
    A.process_interview: E.InterviewDone,  # next hop: INTERVIEWED -> evaluate_candidate
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
    if action in (A.send_outreach, A.send_followup):
        return {"contact_log": mem.contact_log + [{
            "kind": "email_out", "ts": repo.now_iso(), "to": result["to"],
            "subject": result["subject"], "body": result["body"]}]}
    if action == A.process_interview:
        return {"contact_log": mem.contact_log + [{
            "kind": "interview", "ts": repo.now_iso(), "turns": result["transcript"].get("turns") or []}]}
    if action == A.reactivate:
        return {"contact_log": mem.contact_log + [{"kind": "reactivated", "ts": repo.now_iso()}]}
    if action == A.process_reply:
        log = mem.contact_log + [{"kind": "email_in", "ts": repo.now_iso(), "body": result["body"]}]
        if result.get("draft"):
            log.append({"kind": "draft_reply", "ts": repo.now_iso(), **result["draft"]})
        return {"contact_log": log}
    return {}


def _iso_in(seconds: float) -> str:
    d = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


# ---------- lifecycle rules (code decides what is allowed) ----------

REDRAFT_LIMIT = 3          # rejected drafts we redraft before handing over to a human
INTERVIEW_REJECT_LIMIT = 2  # CEO rejected the interview request this many times -> escalate
_DONE_STATES = {S.SHORTLISTED.value, S.REJECTED.value, S.DECLINED.value, S.STALE.value}


def _ts(entry: dict) -> Optional[datetime]:
    try:
        return repo._parse_ts(entry["ts"])
    except (KeyError, ValueError, TypeError):
        return None


def _tick_due(mem: CandidateMemory, now: datetime) -> Optional[str]:
    """'followup' | 'stale' | None for a CONTACTED candidate, from its contact log."""
    outs = [t for e in mem.contact_log if e.get("kind") == "email_out" and (t := _ts(e))]
    if not outs:
        return None
    ins = [t for e in mem.contact_log if e.get("kind") == "email_in" and (t := _ts(e))]
    if ins and max(ins) > max(outs):
        return None  # they replied
    clock = max(outs + [t for e in mem.contact_log if e.get("kind") == "reactivated" and (t := _ts(e))])
    age_h = (now - clock).total_seconds() / 3600
    if len(outs) == 1:
        return "followup" if age_h >= settings.followup_after_hours else None
    return "stale" if age_h >= settings.stale_after_hours else None


def _job_tick(job: dict) -> None:
    """Job-level Tick: completion check, then flag CONTACTED candidates due a nudge / stale."""
    if job["state"] != JobState.ACTIVE.value:
        return
    cands = repo.list_candidates(job["id"])
    shortlisted = sum(c["state"] == S.SHORTLISTED.value for c in cands)
    if shortlisted >= job["shortlist_target"] or (cands and all(c["state"] in _DONE_STATES for c in cands)):
        repo.set_job_state(job["id"], JobState.COMPLETE)
        return
    now = datetime.now(timezone.utc)
    for c in cands:
        if c["state"] != S.CONTACTED.value:
            continue
        due = _tick_due(repo.get_memory(c["id"]), now)
        if due and not repo.has_pending_event(job["id"], E.Tick, c["id"], due=due):
            repo.emit_event(job["id"], E.Tick, c["id"], {"due": due})


def _contextual_actions(cand: dict, job: dict, ev: Event, valid: list[A]) -> tuple[list[A], Optional[dict]]:
    """Narrow the state table by context. Returns (actions, escalation) where escalation is
    {"reason", "extra"} when the run must hand over to a human instead of deciding."""
    state, cid = S(cand["state"]), cand["id"]
    from api.config import get_settings
    if get_settings().mode_for("browser") != "real":  # browser tool off: never offer it
        valid = [a for a in valid if a != A.browser_research]
    if state == S.INTERVIEWING and ev.type not in (E.InterviewDone, E.HumanResolved):
        # Spec 01: wait until the transcript exists (webhook or poller emits InterviewDone with it).
        valid = [a for a in valid if a not in (A.process_interview,)]
    if ev.type == E.Tick and state == S.CONTACTED:
        drop = {"followup": A.mark_stale, "stale": A.send_followup}.get(ev.payload.get("due"))
        valid = [a for a in valid if a != drop]
    if ev.type == E.ApprovalRejected:
        action = ev.payload.get("action")
        if action == A.send_outreach.value and repo.count_approvals(
                cid, A.send_outreach, "rejected") > REDRAFT_LIMIT:
            return valid, {"reason": f"outreach draft rejected {REDRAFT_LIMIT + 1} times", "extra": {}}
        if action == A.schedule_interview.value and repo.count_approvals(
                cid, A.schedule_interview, "rejected") >= INTERVIEW_REJECT_LIMIT:
            return valid, {"reason": "interview request rejected twice", "extra": {}}
    if state == S.EVALUATED and cand.get("score") is not None:
        evaluation = repo.latest_evaluation(cid)
        borderline = bool(evaluation) and evaluation["recommendation"] == "borderline"
        if borderline and ev.type == E.Evaluated:
            return valid, {"reason": "borderline evaluation needs a human decision", "extra": {
                "evaluation": {k: evaluation[k] for k in ("score", "recommendation", "summary")}}}
        if repo.count_approvals(cid, A.reject_candidate_proposal, "rejected"):  # CEO override
            valid = [a for a in valid if a != A.reject_candidate_proposal]
        elif not borderline:
            drop = A.reject_candidate_proposal if cand["score"] >= job["score_threshold"] else A.shortlist
            valid = [a for a in valid if a != drop]
    return valid, None


# ---------- graph ----------

class _Deps:
    def __init__(self, tools: Any, llm: Any) -> None:
        self.tools, self.llm = tools, llm


def build_graph(tools: Any = None, llm: Any = None, checkpointer: Any = None,
                trace: tracing.RunTrace = tracing.NOOP):
    deps = _Deps(tools, llm)

    async def observe(st: AgentState) -> dict:
        ev = st["event"]
        job = repo.get_job(st["job_id"])
        out: dict = {"job": job}
        if ev.type in NOTIFICATION_EVENTS:
            return {**out, "route": "end"}
        cid = st.get("candidate_id")
        if cid is None:
            if ev.type == E.Tick:
                _job_tick(job)
                return {**out, "route": "end"}
            out["valid_actions"] = job_valid_actions(job) if ev.type == E.JobCreated else []
            out["route"] = "decide" if out["valid_actions"] else "end"
            return out
        cand = repo.get_candidate(cid)
        state = S(cand["state"])
        out.update(candidate=cand, memory=repo.get_memory(cid))
        # Escalations/failures already put the candidate in front of a human; don't loop on them.
        if state == S.NEEDS_HUMAN and ev.type != E.HumanResolved:
            out["route"] = "end"
            return out
        out["valid_actions"], handover = _contextual_actions(cand, job, ev, valid_actions(state, job))
        if handover:
            return {**out, "route": "escalate", "escalate_reason": handover["reason"],
                    "escalate_extra": handover["extra"]}
        if ev.type == E.ApprovalGranted:
            # Execute exactly what the CEO approved; the LLM is not consulted.
            ap = repo.get_approval(ev.payload.get("approval_id") or "")
            if ap is not None and ap.status == "executed" and _send_result_recorded(ev, ap):
                ap_ok = True  # crashed after the send, before the state update: finish, don't re-send
            else:
                ap_ok = ap is not None and ap.status == "approved"
            if (not ap_ok or ap.candidate_id != cid or ap.action not in out["valid_actions"]):
                out["route"] = "end"  # replay, rejected, already executed, or no longer applicable
            else:
                out.update(approval=ap, route="execute", decision=Decision(
                    action=ap.action, args=ap.payload, reasoning=ap.reasoning or "approved by CEO"))
            return out
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
        llm = _traced_llm(llm, trace)
        outcome = await decide_validated(llm, state, st["job"], valid=st["valid_actions"],
                                         error=st.get("redecide_error"))
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
        out = await _permission_check({**st, "decision": _with_default_args(st, st["decision"])})
        return {"decision": _with_default_args(st, st["decision"]), **out}

    async def _permission_check(st: AgentState) -> dict:
        d, cid = st["decision"], st["candidate_id"]
        if (refusal := _refuse_duplicate_send(st)) is not None:
            return refusal  # don't even ask the CEO to approve a send that would be refused
        verdict = check_permission(d.action, st["job"].get("permission_policy"))
        if verdict == Verdict.allow:
            return {"route": "execute"}
        ap = repo.get_approved_approval(cid, d.action)  # approved but not yet executed: use it as stored
        if ap is not None:
            return {"approval": ap, "route": "execute"}
        if verdict == Verdict.auto_approve:
            aid = repo.create_approval(st["job_id"], cid, d.action, d.args, d.reasoning,
                                       status="approved", decided_by="auto")
            if d.action == A.send_outreach and st["candidate"]["state"] == S.OUTREACH_DRAFTED.value:
                repo.set_candidate_state(cid, S.APPROVED)  # same durable state as a CEO approval
            repo.emit_event(st["job_id"], E.ApprovalGranted, cid,
                            {"approval_id": aid, "action": d.action.value, "auto": True})
            return {"approval": repo.get_approval(aid), "candidate": repo.get_candidate(cid),
                    "route": "execute"}
        if repo.get_pending_approval(cid, d.action):
            return {"route": "end"}  # already waiting on the CEO; don't stack requests
        aid = repo.create_approval(st["job_id"], cid, d.action, d.args, d.reasoning)
        repo.emit_event(st["job_id"], E.ApprovalRequested, cid,
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
        if (refusal := _refuse_duplicate_send(st)) is not None:  # approvals skip permission_check
            return refusal
        tools = deps.tools
        if tools is None:
            from api.tools import get_tools
            tools = deps.tools = get_tools()

        async def run(payload: Optional[dict] = None) -> dict:
            # Restricted actions run on the stored approval payload, never on fresh LLM args.
            s2 = st if payload is None else {**st, "decision": d.model_copy(update={"args": payload})}
            return await _run_with_retries(tool, fn, tools, s2, key)

        try:
            if check_permission(d.action, st["job"].get("permission_policy")) == Verdict.allow:
                result = await run()
            else:  # defense in depth: restricted tools re-check the approval themselves
                ap = st.get("approval")
                assert_approved(ap, d.action)
                result = await execute_approved(ap.id, run)
        except PermissionDenied:
            return {"route": "end"}
        except ToolError as e:
            if e.kind == "invalid_input" and not st.get("tool_redecided") and st.get("approval") is None:
                # Counts as a decide-error: one re-decide with the tool's complaint, then escalate.
                return {"tool_redecided": True, "decision": None, "route": "decide",
                        "redecide_error": f"{d.action.value} was rejected by the tool: {e}"}
            if e.kind == "not_found" and st.get("candidate_id") and d.action not in NOT_FOUND_ESCALATES:
                # No retry, no escalation: leave a note; the next decision starts from memory.
                notes = {**st["memory"].research_notes, f"{d.action.value}_error": f"not_found: {e}"}
                repo.update_memory(st["candidate_id"], research_notes=notes)
                return {"error": {"kind": e.kind, "message": str(e)}, "route": "end"}
            return _fail(st, e, getattr(e, "attempts", 1))
        return {"result": result, "route": "update_state"}

    async def _run_with_retries(tool: str, fn, tools, st: AgentState, key: str) -> dict:
        d: Decision = st["decision"]
        started, attempt = time.monotonic(), 0
        while True:
            attempt += 1
            try:
                with trace.span(d.action.value, kind="tool", input=d.args) as sp:
                    result = await fn(tools, st)
                    sp.set_output(result)
            except ToolError as e:
                delay = _retry_delay(e, attempt)
                if delay is None:
                    repo.record_tool_call(
                        run_id=st["run_id"], candidate_id=st.get("candidate_id"), tool=tool,
                        action=d.action, idempotency_key=key, input=d.args, output=None,
                        status="error", error_kind=e.kind, attempts=attempt)
                    e.attempts = attempt
                    raise
                await asyncio.sleep(delay)
                continue
            repo.record_tool_call(
                run_id=st["run_id"], candidate_id=st.get("candidate_id"), tool=tool, action=d.action,
                idempotency_key=key, input=d.args, output=result, status="ok", attempts=attempt,
                latency_ms=int((time.monotonic() - started) * 1000))
            return result

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
        _record_messages(st, d.action, result, cid, job)
        _record_lifecycle(d.action, result, cid, job)
        if d.action in NEXT_EVENT:
            repo.emit_event(st["job_id"], NEXT_EVENT[d.action], cid, {"from": cand["state"]})
        return {}

    def _record_lifecycle(action: A, result: dict, cid: str, job: dict) -> None:
        if action == A.schedule_interview:
            sess = result["session"]
            repo.create_interview(cid, job["id"], conversation_id=sess.get("conversation_id"),
                                  join_url=sess.get("join_url"), questions=result.get("questions") or [],
                                  expires_at=sess.get("expires_at"))
            repo.emit_event(job["id"], E.InterviewScheduled, cid, {
                "join_url": sess.get("join_url"), "conversation_id": sess.get("conversation_id")})
        elif action == A.process_interview:
            repo.complete_interview(cid, job["id"], result["transcript"])
        elif action == A.evaluate_candidate:
            ev = result["evaluation"]
            repo.add_evaluation(cid, job["id"], ev)
            trace.score("evaluation_score", ev["score"])
            repo.emit_event(job["id"], E.Evaluated, cid, {
                "score": ev["score"], "recommendation": ev["recommendation"]})

    def _record_messages(st: AgentState, action: A, result: dict, cid: str, job: dict) -> None:
        if action in (A.send_outreach, A.send_followup):
            repo.add_message(cid, "out", result["body"], subject=result["subject"],
                             thread_id=result["thread_id"])
            repo.emit_event(job["id"], E.EmailSent, cid, {
                "to": result["to"], "subject": result["subject"], "message_id": result["message_id"]})
            if result.get("persona_reply"):  # delayed, so the demo shows the agent waiting
                repo.emit_event(job["id"], E.EmailReplied, cid, {"body": result["persona_reply"]},
                                not_before=_iso_in(settings.persona_delay_s))
        elif action == A.process_reply:
            repo.add_message(cid, "in", result["body"], thread_id=f"thread-{cid}")

    async def escalate(st: AgentState) -> dict:
        reason = st.get("escalate_reason") or "escalated"
        if st.get("candidate_id"):
            repo.escalate_candidate(st["candidate_id"], reason, trace_url=st.get("trace_url"),
                                    extra=st.get("escalate_extra"))
        else:
            repo.emit_event(st["job_id"], E.Escalated, None,
                            {"reason": reason, "trace_url": st.get("trace_url")})
        return {}

    g = StateGraph(AgentState)
    for name, fn in [("observe", observe), ("decide", decide), ("permission_check", permission_check),
                     ("execute", execute), ("update_state", update_state), ("escalate", escalate)]:
        g.add_node(name, _traced_node(name, fn, trace))
    g.add_edge(START, "observe")
    for src, targets in {
        "observe": {"decide", "execute", "escalate"}, "decide": {"permission_check", "escalate"},
        "permission_check": {"execute"}, "execute": {"update_state", "escalate", "decide"},
    }.items():
        g.add_conditional_edges(
            src, lambda s, ok=targets: s["route"] if s["route"] in ok else END,
            {**{t: t for t in targets}, END: END})
    g.add_edge("update_state", END)
    g.add_edge("escalate", END)
    return g.compile(checkpointer=checkpointer)


def _send_result_recorded(ev: Event, ap: Any) -> bool:
    """Did this event already run the approved action successfully (tool result on record)?"""
    prior = repo.get_tool_call(f"{ev.id}:{ap.action.value}:{ap.candidate_id}")
    return bool(prior and prior["status"] == "ok")


def _duplicate_send_reason(mem: CandidateMemory, d: Decision) -> Optional[str]:
    """Did we already contact this person? (specs/04-data-model.md rule 3)"""
    sent = [e for e in mem.contact_log if e.get("kind") == "email_out"]
    if d.action == A.send_outreach and sent:
        return f"already contacted ({len(sent)} email sent, last {sent[-1].get('ts')})"
    if d.action == A.send_followup and any(
            e.get("subject") == d.args.get("subject") and e.get("body") == d.args.get("body")
            for e in sent):
        return "an identical message was already sent"
    return None


def _refuse_duplicate_send(st: AgentState) -> Optional[dict]:
    """Refuse a duplicate send: nothing is sent, the refusal is logged, the run ends."""
    d, cid = st["decision"], st.get("candidate_id")
    if cid is None or d.action not in (A.send_outreach, A.send_followup):
        return None
    reason = _duplicate_send_reason(repo.get_memory(cid), d)  # fresh: a send may have landed since observe
    if reason is None:
        return None
    outcome = f"refused: {reason}"
    state = S(repo.get_candidate(cid)["state"])
    if d.action == A.send_outreach and state in (S.OUTREACH_DRAFTED, S.APPROVED):
        repo.set_candidate_state(cid, S.CONTACTED)  # they were contacted already: don't re-draft or re-ask
        outcome += f" ({state.value} -> CONTACTED)"
    repo.append_decision(cid, {"ts": repo.now_iso(), "action": d.action.value,
                               "reasoning": d.reasoning, "outcome": outcome})
    return {"route": "end"}


def _traced_node(name: str, fn, trace: tracing.RunTrace):
    async def run(st: AgentState) -> dict:
        with trace.span(name) as sp:
            out = await fn(st)
            sp.set_output({"route": out.get("route")} if isinstance(out, dict) else None)
            return out
    return run


def _traced_llm(llm, trace: tracing.RunTrace):
    """One `generation` per decide call, scored decision_valid (1 = the action was offered)."""
    async def call(valid, error=None):
        names = [a.value for a in valid]
        with trace.span("decide", kind="generation",
                        input={"valid_actions": names, "error": error}) as g:
            d = await llm(valid, error)
            g.set_output({"action": d.action.value, "reasoning": d.reasoning})
        ok = d.action in valid
        trace.score("decision_valid", 1 if ok else 0,
                    None if ok else f"{d.action.value} not in valid actions")
        return d
    return call


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
    lock_owner = f"run-{ULID()}" if cid and owner is None else None
    if lock_owner and not await asyncio.to_thread(repo.acquire_lock, cid, lock_owner):
        repo.finish_run(run_id, "error", None, "candidate locked by another owner")
        raise repo.LockBusy(f"candidate {cid} is locked by another owner")
    status, err, final = "ok", None, {}
    trace = tracing.NOOP
    try:
        cand = repo.get_candidate(cid) if cid else None
        trace = await asyncio.to_thread(  # may do network I/O; keep it off the event loop
            tracing.start_run_trace, job_id=event.job_id, candidate_id=cid, state=cand["state"] if cand else "job",
            event_type=event.type.value)
    except Exception:  # noqa: BLE001 - tracing is best-effort
        pass
    try:
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            saver.serde = _SERDE
            app = build_graph(tools, llm, saver, trace)
            tid = thread_id_for(event.job_id, cid)
            final = await app.ainvoke(
                # The thread is reused for every event of a candidate and LangGraph restores the
                # last checkpoint, so every per-run field must be reset explicitly.
                {"job_id": event.job_id, "candidate_id": cid, "event": event,
                 "decide_attempts": 0, "run_id": run_id, "route": "decide",
                 "decision": None, "result": None, "error": None, "approval": None,
                 "escalate_reason": "", "escalate_extra": {}, "valid_actions": [],
                 "tool_redecided": False, "redecide_error": None, "trace_url": trace.url},
                config={"configurable": {"thread_id": tid}})
            # Finished cleanly: drop this thread's history. Checkpoints of a crashed run stay.
            await saver.adelete_thread(tid)
        if final.get("error") or final.get("route") == "escalate":
            status = "error"
        return {**final, "run_id": run_id}
    except BaseException as e:  # noqa: BLE001 - recorded, then re-raised for the dispatcher
        status, err = "error", repr(e)
        raise
    finally:
        if lock_owner:
            await asyncio.to_thread(repo.release_lock, cid, lock_owner)
        await asyncio.to_thread(trace.flush)
        d = final.get("decision")
        repo.finish_run(run_id, status, d.action.value if d else None, err,
                        trace_url=trace.url, trace_id=trace.trace_id)


async def handle_event(event: Event, owner: Optional[str] = None) -> None:
    """Dispatcher handler (default tools and LLM from config)."""
    await run_event(event, owner=owner)
