"""Red tests for the permission layer (specs/03-permissions.md; lane A checklist 1:10-1:40).

Covers testing.md item 4: "RESTRICTED actions only run from an approved approval row, at most once".

Assumed API in api/agent/permissions.py (implement to match):
    class Verdict(str, Enum): allow / auto_approve / require_approval
    check_permission(action, policy) -> Verdict
        `policy` is a job's permission_policy dict (or repo.PermissionPolicy).
        - action not RESTRICTED, or not in policy.restricted_actions -> allow
        - RESTRICTED and auto_approve applies -> auto_approve
            (auto_approve=True and action in auto_approve_actions, or the list is empty;
             reject_candidate_proposal only when listed explicitly)
        - otherwise -> require_approval
    class PermissionDenied(Exception)
    assert_approved(approval, action) -> None
        raises PermissionDenied unless approval is not None, status == "approved" and
        approval.action == action
    async execute_approved(approval_id, fn) -> Any
        Loads the approval, asserts approved, atomically claims it (approved -> executed),
        then awaits fn(stored_payload). Raises PermissionDenied (fn never called) for a missing,
        pending, rejected, expired or already-executed approval.

Assumed behaviour in api/agent/graph.py:
    - permission_check: RESTRICTED + require_approval -> pending approval + ApprovalRequested, run ends.
    - RESTRICTED + auto_approve -> approval row (decided_by="auto") + ApprovalGranted, then execute
      from the stored payload in the same run; the approval ends `executed`.
    - observe on ApprovalGranted loads the approval and jumps straight to execute with its stored
      payload (the LLM is not consulted); replaying the event never executes twice.
    - send_outreach executes via tools.email.send(to, subject, body, thread_id).
"""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from api.agent import graph, permissions
from api.contracts import (
    ActionName as A, Approval, CandidateState as S, Decision, Event, EventType as E, JobState,
)
from api.db import repo
from api.tools.models import Draft

RESTRICTED = [A.send_outreach, A.send_followup, A.schedule_interview, A.reject_candidate_proposal]
# (state in which the action is valid, action)
RESTRICTED_AT = [
    (S.OUTREACH_DRAFTED, A.send_outreach),
    (S.CONTACTED, A.send_followup),
    (S.INTERESTED, A.schedule_interview),
    (S.EVALUATED, A.reject_candidate_proposal),
]


# ---------- fixtures / fakes ----------

@pytest.fixture
def db(tmp_path):
    repo.init_db(tmp_path / "recruiter.db")
    return repo


def make_job(**policy):
    jid = repo.create_job("Backend Eng", {}, permission_policy=policy or None)
    repo.set_job_state(jid, JobState.ACTIVE)
    return jid


def make_candidate(job, state=S.OUTREACH_DRAFTED):
    cid = repo.create_candidate(job, "p1", "Ada Lovelace", state=state)
    repo.update_memory(cid, contact={"email": "ada@example.com", "confidence": 0.9, "source": "fake"})
    return cid


class FakeEmail:
    def __init__(self):
        self.sent = []

    async def send(self, to, subject, body, thread_id):
        self.sent.append({"to": to, "subject": subject, "body": body, "thread_id": thread_id})
        return SimpleNamespace(message_id=f"sim_{len(self.sent)}", to=to, thread_id=thread_id,
                               simulated=True)


class FakeDrafter:
    def __init__(self):
        self.feedback = []

    async def outreach(self, job, memory, feedback=None):
        self.feedback.append(feedback)
        return Draft(subject="Redraft", body="redrafted body")


def tools_with(email=None, drafter=None):
    return SimpleNamespace(email=email or FakeEmail(), drafter=drafter or FakeDrafter())


def scripted(action, **args):
    calls = []

    async def llm(valid, error=None):
        calls.append(list(valid))
        return Decision(action=action, args=args, reasoning="because")

    llm.calls = calls
    return llm


def never_called_llm():
    async def llm(valid, error=None):  # pragma: no cover - failing the test is the point
        raise AssertionError("LLM must not be consulted when executing an approved action")
    llm.calls = []
    return llm


def event(job, type_, candidate_id=None, **payload):
    eid = repo.emit_event(job, type_, candidate_id=candidate_id, payload=payload)
    return Event(id=eid, job_id=job, candidate_id=candidate_id, type=type_, payload=payload)


def events_of(job, type_):
    with repo.get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM events WHERE job_id=? AND type=? ORDER BY id", (job, type_.value))]


def approvals_of(cid):
    with repo.get_conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM approvals WHERE candidate_id=?", (cid,))]


def state_of(cid):
    return S(repo.get_candidate(cid)["state"])


APPROVED_PAYLOAD = {"subject": "Hello Ada", "body": "Approved body"}


def pending_send_approval(job, cid, payload=None):
    return repo.create_approval(job, cid, A.send_outreach, payload or dict(APPROVED_PAYLOAD),
                                "draft looks good")


# ---------- check_permission: the capability matrix ----------

@pytest.mark.parametrize("action", [
    A.search_candidates, A.enrich_candidate, A.find_contact, A.browser_research,
    A.draft_outreach, A.redraft_outreach, A.draft_reply, A.evaluate_candidate,
    A.shortlist, A.mark_stale, A.escalate, A.wait,
])
def test_read_and_write_actions_are_always_allowed(action):
    assert permissions.check_permission(action, {"auto_approve": False}) == permissions.Verdict.allow


@pytest.mark.parametrize("action", RESTRICTED)
def test_default_policy_requires_approval_for_every_restricted_action(action):
    assert permissions.check_permission(action, {}) == permissions.Verdict.require_approval
    assert permissions.check_permission(action, repo.PermissionPolicy()) == permissions.Verdict.require_approval


@pytest.mark.parametrize("action", [A.send_outreach, A.send_followup, A.schedule_interview])
def test_auto_approve_with_empty_list_approves_all_but_rejection(action):
    policy = {"auto_approve": True, "auto_approve_actions": []}
    assert permissions.check_permission(action, policy) == permissions.Verdict.auto_approve


def test_reject_candidate_proposal_is_never_auto_approved_by_the_blanket_toggle():
    policy = {"auto_approve": True, "auto_approve_actions": []}
    assert permissions.check_permission(A.reject_candidate_proposal, policy) == permissions.Verdict.require_approval


def test_reject_candidate_proposal_auto_approved_only_when_listed_explicitly():
    policy = {"auto_approve": True, "auto_approve_actions": [A.reject_candidate_proposal]}
    assert permissions.check_permission(A.reject_candidate_proposal, policy) == permissions.Verdict.auto_approve


def test_reject_candidate_proposal_listed_but_auto_approve_off_still_needs_approval():
    policy = {"auto_approve": False, "auto_approve_actions": [A.reject_candidate_proposal]}
    assert permissions.check_permission(A.reject_candidate_proposal, policy) == permissions.Verdict.require_approval


def test_auto_approve_actions_list_limits_which_actions_are_approved():
    policy = {"auto_approve": True, "auto_approve_actions": [A.send_outreach]}
    assert permissions.check_permission(A.send_outreach, policy) == permissions.Verdict.auto_approve
    assert permissions.check_permission(A.send_followup, policy) == permissions.Verdict.require_approval
    assert permissions.check_permission(A.schedule_interview, policy) == permissions.Verdict.require_approval


def test_action_removed_from_restricted_actions_is_allowed():
    """Spec 03: moving the boundary is a one-line config change."""
    policy = {"restricted_actions": [A.send_outreach, A.schedule_interview, A.reject_candidate_proposal]}
    assert permissions.check_permission(A.send_followup, policy) == permissions.Verdict.allow
    assert permissions.check_permission(A.send_outreach, policy) == permissions.Verdict.require_approval


def test_policy_accepts_string_action_names_from_stored_json():
    policy = {"auto_approve": True, "auto_approve_actions": ["send_outreach"]}
    assert permissions.check_permission(A.send_outreach, policy) == permissions.Verdict.auto_approve


# ---------- assert_approved: defense in depth ----------

def _approval(status="approved", action=A.send_outreach):
    return Approval(id="a1", job_id="j", candidate_id="c", action=action, status=status)


def test_assert_approved_passes_for_matching_approved_row():
    permissions.assert_approved(_approval(), A.send_outreach)


@pytest.mark.parametrize("status", ["pending", "rejected", "expired", "executed"])
def test_assert_approved_refuses_any_non_approved_status(status):
    with pytest.raises(permissions.PermissionDenied):
        permissions.assert_approved(_approval(status), A.send_outreach)


def test_assert_approved_refuses_missing_approval():
    with pytest.raises(permissions.PermissionDenied):
        permissions.assert_approved(None, A.send_outreach)


def test_assert_approved_refuses_approval_for_a_different_action():
    with pytest.raises(permissions.PermissionDenied):
        permissions.assert_approved(_approval(action=A.send_followup), A.send_outreach)


# ---------- execute_approved: only from an approved row, at most once ----------

async def test_execute_approved_runs_with_the_stored_payload_and_marks_executed(db):
    job = make_job()
    cid = make_candidate(job)
    aid = pending_send_approval(job, cid)
    repo.decide_approval(aid, True)
    seen = []

    async def fn(payload):
        seen.append(payload)
        return {"ok": True}

    out = await permissions.execute_approved(aid, fn)
    assert out == {"ok": True}
    assert seen == [APPROVED_PAYLOAD]
    ap = repo.get_approval(aid)
    assert ap.status == "executed" and ap.executed_at


async def test_execute_approved_uses_the_ceo_edited_payload(db):
    job = make_job()
    cid = make_candidate(job)
    aid = pending_send_approval(job, cid)
    edited = {"subject": "Edited", "body": "CEO rewrote this"}
    repo.decide_approval(aid, True, payload=edited)
    seen = []

    async def fn(payload):
        seen.append(payload)

    await permissions.execute_approved(aid, fn)
    assert seen == [edited]


@pytest.mark.parametrize("decision", [None, "reject"])
async def test_execute_approved_refuses_pending_and_rejected_rows(db, decision):
    job = make_job()
    cid = make_candidate(job)
    aid = pending_send_approval(job, cid)
    if decision == "reject":
        repo.decide_approval(aid, False, feedback="no")
    fn_calls = []

    async def fn(payload):
        fn_calls.append(payload)

    with pytest.raises(permissions.PermissionDenied):
        await permissions.execute_approved(aid, fn)
    assert fn_calls == []
    assert repo.get_approval(aid).status in ("pending", "rejected")


async def test_execute_approved_refuses_unknown_approval(db):
    async def fn(payload):  # pragma: no cover
        raise AssertionError("must not run")

    with pytest.raises(permissions.PermissionDenied):
        await permissions.execute_approved("does-not-exist", fn)


async def test_execute_approved_second_call_is_refused(db):
    job = make_job()
    cid = make_candidate(job)
    aid = pending_send_approval(job, cid)
    repo.decide_approval(aid, True)
    fn_calls = []

    async def fn(payload):
        fn_calls.append(payload)

    await permissions.execute_approved(aid, fn)
    with pytest.raises(permissions.PermissionDenied):
        await permissions.execute_approved(aid, fn)
    assert len(fn_calls) == 1


async def test_execute_approved_concurrent_callers_run_it_exactly_once(db):
    job = make_job()
    cid = make_candidate(job)
    aid = pending_send_approval(job, cid)
    repo.decide_approval(aid, True)
    fn_calls = []

    async def fn(payload):
        await asyncio.sleep(0.01)
        fn_calls.append(payload)

    results = await asyncio.gather(
        *(permissions.execute_approved(aid, fn) for _ in range(6)), return_exceptions=True)
    assert len(fn_calls) == 1
    assert sum(isinstance(r, permissions.PermissionDenied) for r in results) == 5


def test_mark_approval_executed_is_atomic_across_threads(db):
    job = make_job()
    cid = make_candidate(job)
    aid = pending_send_approval(job, cid)
    repo.decide_approval(aid, True)
    wins, barrier = [], threading.Barrier(8)

    def worker():
        barrier.wait()
        wins.append(repo.mark_approval_executed(aid))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


# ---------- graph: permission_check parks restricted actions ----------

@pytest.mark.parametrize("state,action", RESTRICTED_AT)
async def test_restricted_action_creates_pending_approval_and_ends_the_run(db, state, action):
    job = make_job()
    cid = make_candidate(job, state)
    tools = tools_with()
    args = {"subject": "Hi", "body": "Draft body"}
    await graph.run_event(event(job, E.Tick, cid), tools=tools, llm=scripted(action, **args))
    rows = approvals_of(cid)
    assert len(rows) == 1
    assert rows[0]["action"] == action.value and rows[0]["status"] == "pending"
    assert len(events_of(job, E.ApprovalRequested)) == 1
    assert tools.email.sent == []
    assert state_of(cid) == state  # nothing moved without the CEO


async def test_pending_approval_stores_the_decided_args_as_payload(db):
    job = make_job()
    cid = make_candidate(job)
    await graph.run_event(event(job, E.Tick, cid), tools=tools_with(),
                          llm=scripted(A.send_outreach, subject="Hi", body="Draft body"))
    row = repo.get_pending_approval(cid, A.send_outreach)
    assert row.payload == {"subject": "Hi", "body": "Draft body"}
    assert row.reasoning == "because"


# ---------- graph: auto-approve ----------

async def test_auto_approve_sends_from_the_stored_payload_and_marks_executed(db):
    job = make_job(auto_approve=True)
    cid = make_candidate(job)
    tools = tools_with()
    await graph.run_event(event(job, E.Tick, cid), tools=tools,
                          llm=scripted(A.send_outreach, subject="Hi", body="Auto body"))
    assert [(m["subject"], m["body"]) for m in tools.email.sent] == [("Hi", "Auto body")]
    (row,) = approvals_of(cid)
    assert row["status"] == "executed" and row["decided_by"] == "auto"
    assert state_of(cid) == S.CONTACTED


async def test_auto_approve_keeps_the_audit_trail(db):
    job = make_job(auto_approve=True)
    cid = make_candidate(job)
    await graph.run_event(event(job, E.Tick, cid), tools=tools_with(),
                          llm=scripted(A.send_outreach, subject="Hi", body="Auto body"))
    assert len(approvals_of(cid)) == 1
    assert len(events_of(job, E.ApprovalGranted)) == 1


async def test_auto_approve_is_read_from_the_job_on_every_check(db):
    job = make_job(auto_approve=False)
    cid = make_candidate(job)
    tools = tools_with()
    repo.update_job(job, permission_policy={"auto_approve": True})  # flipped while "running"
    await graph.run_event(event(job, E.Tick, cid), tools=tools,
                          llm=scripted(A.send_outreach, subject="Hi", body="b"))
    assert len(tools.email.sent) == 1


async def test_auto_approve_actions_list_leaves_other_restricted_actions_pending(db):
    job = make_job(auto_approve=True, auto_approve_actions=["send_outreach"])
    cid = make_candidate(job, S.CONTACTED)
    tools = tools_with()
    await graph.run_event(event(job, E.Tick, cid), tools=tools,
                          llm=scripted(A.send_followup, subject="Re", body="nudge"))
    (row,) = approvals_of(cid)
    assert row["status"] == "pending"
    assert tools.email.sent == []


async def test_reject_candidate_proposal_stays_pending_under_blanket_auto_approve(db):
    job = make_job(auto_approve=True)
    cid = make_candidate(job, S.EVALUATED)
    await graph.run_event(event(job, E.Tick, cid), tools=tools_with(),
                          llm=scripted(A.reject_candidate_proposal, reason="score too low"))
    (row,) = approvals_of(cid)
    assert row["action"] == "reject_candidate_proposal" and row["status"] == "pending"
    assert state_of(cid) == S.EVALUATED


async def test_reject_candidate_proposal_auto_approved_when_listed_explicitly(db):
    job = make_job(auto_approve=True, auto_approve_actions=["reject_candidate_proposal"])
    cid = make_candidate(job, S.EVALUATED)
    await graph.run_event(event(job, E.Tick, cid), tools=tools_with(),
                          llm=scripted(A.reject_candidate_proposal, reason="score too low"))
    (row,) = approvals_of(cid)
    assert row["status"] == "executed" and row["decided_by"] == "auto"
    assert state_of(cid) == S.REJECTED


# ---------- graph: ApprovalGranted -> execute the stored payload ----------

async def test_approval_granted_executes_the_stored_payload_without_asking_the_llm(db):
    job = make_job()
    cid = make_candidate(job)
    aid = pending_send_approval(job, cid)
    repo.decide_approval(aid, True)
    assert state_of(cid) == S.APPROVED  # APPROVED is durable between approval and send
    tools, llm = tools_with(), never_called_llm()
    granted = events_of(job, E.ApprovalGranted)[0]
    await graph.run_event(Event(id=granted["id"], job_id=job, candidate_id=cid,
                                type=E.ApprovalGranted, payload={"approval_id": aid}),
                          tools=tools, llm=llm)
    assert [(m["subject"], m["body"]) for m in tools.email.sent] == [("Hello Ada", "Approved body")]
    assert state_of(cid) == S.CONTACTED
    assert repo.get_approval(aid).status == "executed"


async def test_ceo_edited_payload_is_what_gets_sent(db):
    job = make_job()
    cid = make_candidate(job)
    aid = pending_send_approval(job, cid)
    edited = {"subject": "Edited subject", "body": "Edited by the CEO"}
    repo.decide_approval(aid, True, payload=edited)
    tools = tools_with()
    granted = events_of(job, E.ApprovalGranted)[0]
    await graph.run_event(Event(id=granted["id"], job_id=job, candidate_id=cid,
                                type=E.ApprovalGranted, payload={"approval_id": aid}),
                          tools=tools, llm=never_called_llm())
    assert [(m["subject"], m["body"]) for m in tools.email.sent] == [("Edited subject", "Edited by the CEO")]


async def test_replayed_approval_granted_event_sends_only_once(db):
    job = make_job()
    cid = make_candidate(job)
    aid = pending_send_approval(job, cid)
    repo.decide_approval(aid, True)
    tools = tools_with()
    granted = events_of(job, E.ApprovalGranted)[0]
    ev = Event(id=granted["id"], job_id=job, candidate_id=cid, type=E.ApprovalGranted,
               payload={"approval_id": aid})
    await graph.run_event(ev, tools=tools, llm=never_called_llm())
    await graph.run_event(ev, tools=tools, llm=scripted(A.wait))  # replay (crash/retry/duplicate)
    assert len(tools.email.sent) == 1
    assert repo.get_approval(aid).status == "executed"


async def test_second_granted_event_for_an_already_executed_approval_is_a_noop(db):
    job = make_job()
    cid = make_candidate(job)
    aid = pending_send_approval(job, cid)
    repo.decide_approval(aid, True)
    tools = tools_with()
    for _ in range(2):
        ev = event(job, E.ApprovalGranted, cid, approval_id=aid, action="send_outreach")
        await graph.run_event(ev, tools=tools, llm=scripted(A.wait))
    assert len(tools.email.sent) == 1


# ---------- graph: rejection, and no bypass ----------

async def test_rejected_approval_never_sends_and_stays_rejected(db):
    job = make_job()
    cid = make_candidate(job)
    aid = pending_send_approval(job, cid)
    repo.decide_approval(aid, False, feedback="too pushy")
    tools = tools_with()
    rejected = events_of(job, E.ApprovalRejected)[0]
    await graph.run_event(Event(id=rejected["id"], job_id=job, candidate_id=cid,
                                type=E.ApprovalRejected,
                                payload={"approval_id": aid, "feedback": "too pushy"}),
                          tools=tools, llm=scripted(A.redraft_outreach))
    assert tools.email.sent == []
    assert repo.get_approval(aid).status == "rejected"
    assert state_of(cid) == S.OUTREACH_DRAFTED


async def test_rejected_send_outreach_redrafts_with_the_ceo_feedback(db):
    job = make_job()
    cid = make_candidate(job)
    aid = pending_send_approval(job, cid)
    repo.decide_approval(aid, False, feedback="too pushy")
    tools = tools_with()
    rejected = events_of(job, E.ApprovalRejected)[0]
    await graph.run_event(Event(id=rejected["id"], job_id=job, candidate_id=cid,
                                type=E.ApprovalRejected,
                                payload={"approval_id": aid, "feedback": "too pushy"}),
                          tools=tools, llm=scripted(A.redraft_outreach))
    assert tools.drafter.feedback == ["too pushy"]


async def test_send_outreach_without_any_approved_row_never_sends(db):
    """Defense in depth: even a candidate forced into APPROVED (a graph/DB bug) can't send unapproved."""
    job = make_job()
    cid = make_candidate(job, S.APPROVED)  # no approval row at all
    tools = tools_with()
    await graph.run_event(event(job, E.Tick, cid), tools=tools,
                          llm=scripted(A.send_outreach, subject="Hi", body="sneaky"))
    assert tools.email.sent == []
    assert state_of(cid) != S.CONTACTED


async def test_stale_approved_row_of_another_candidate_cannot_authorize_a_send(db):
    job = make_job()
    cid_a = make_candidate(job)
    cid_b = repo.create_candidate(job, "p2", "Grace Hopper", state=S.APPROVED)
    repo.update_memory(cid_b, contact={"email": "grace@example.com", "source": "fake"})
    aid = pending_send_approval(job, cid_a)
    repo.decide_approval(aid, True)  # approves A only
    tools = tools_with()
    await graph.run_event(event(job, E.Tick, cid_b), tools=tools,
                          llm=scripted(A.send_outreach, subject="Hi", body="to B"))
    assert all(m["to"] != "grace@example.com" for m in tools.email.sent)
    assert repo.get_approval(aid).status == "approved"  # A's approval untouched
