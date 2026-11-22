"""Red tests: the contact_log check refuses a duplicate send (specs/04-data-model.md rule 3,
specs/01-state-machine.md "Duplicate guard"; checklist 2:50-3:40).

    Before send_outreach / send_followup the candidate's contact_log is checked
    ("did we already contact this person?").
    - send_outreach is refused when the log already holds an `email_out` entry.
    - send_followup is refused only for an identical recent send (same subject + body).
    - A refusal sends nothing (no email, no messages row, no EmailSent / EmailReplied events),
      requests no approval, does not escalate, and is shown in the activity log: a memory
      `decisions` entry for the action whose outcome starts with "refused".
    - Enforced both before an approval is requested (permission_check) and when an approved
      payload is executed (execute), because ApprovalGranted skips permission_check.
"""
import pytest

from api.agent import graph
from api.contracts import ActionName as A, CandidateState as S, Event, EventType as E
from api.db import repo

from tests.agent.test_permissions import (  # noqa: F401  (fixtures + fakes)
    approvals_of, db, event, events_of, make_candidate, make_job, messages_of, never_called_llm,
    scripted, state_of, tools_with,
)

PRIOR = {"kind": "email_out", "ts": "2026-11-22T10:00:00.000Z", "to": "ada@example.com",
         "subject": "Hello Ada", "body": "Original outreach"}


def with_prior_send(cid, **over):
    repo.update_memory(cid, contact_log=[{**PRIOR, **over}])


def refusals(cid, action):
    return [d for d in repo.get_memory(cid).decisions
            if d.get("action") == action.value and str(d.get("outcome", "")).lower().startswith("refused")]


def assert_nothing_sent(job, cid, tools):
    assert tools.email.sent == []
    assert [m for m in messages_of(cid) if m["direction"] == "out"] == []
    assert events_of(job, E.EmailSent) == [] and events_of(job, E.EmailReplied) == []
    assert state_of(cid) != S.NEEDS_HUMAN and events_of(job, E.Escalated) == []


async def _granted(job, cid, action, payload):
    aid = repo.create_approval(job, cid, action, payload, "ok")
    repo.decide_approval(aid, True)
    ev = events_of(job, E.ApprovalGranted)[0]
    return aid, Event(id=ev["id"], job_id=job, candidate_id=cid, type=E.ApprovalGranted,
                      payload={"approval_id": aid})


# ---------- send_outreach ----------

async def test_duplicate_outreach_is_refused_before_asking_for_approval(db):
    job = make_job()
    cid = make_candidate(job)
    with_prior_send(cid)
    tools = tools_with()
    await graph.run_event(event(job, E.Tick, cid), tools=tools,
                          llm=scripted(A.send_outreach, subject="Hi again", body="Second try"))
    assert approvals_of(cid) == [] and events_of(job, E.ApprovalRequested) == []
    assert_nothing_sent(job, cid, tools)


async def test_duplicate_outreach_is_refused_even_with_auto_approve(db):
    job = make_job(auto_approve=True)
    cid = make_candidate(job)
    with_prior_send(cid)
    tools = tools_with()
    await graph.run_event(event(job, E.Tick, cid), tools=tools,
                          llm=scripted(A.send_outreach, subject="Hi again", body="Second try"))
    assert events_of(job, E.ApprovalGranted) == []
    assert_nothing_sent(job, cid, tools)


async def test_refusal_is_shown_in_the_activity_log(db):
    job = make_job()
    cid = make_candidate(job)
    with_prior_send(cid)
    await graph.run_event(event(job, E.Tick, cid), tools=tools_with(),
                          llm=scripted(A.send_outreach, subject="Hi", body="again"))
    (entry,) = refusals(cid, A.send_outreach)
    assert "already" in entry["outcome"].lower() or "contacted" in entry["outcome"].lower()


async def test_approved_outreach_is_refused_at_execute_when_already_contacted(db):
    """The CEO approved a draft, but the candidate was contacted in the meantime."""
    job = make_job()
    cid = make_candidate(job)
    aid, granted = await _granted(job, cid, A.send_outreach, {"subject": "Hello", "body": "Late approval"})
    with_prior_send(cid)  # a send landed after the approval was requested
    tools = tools_with()
    await graph.run_event(granted, tools=tools, llm=never_called_llm())
    assert_nothing_sent(job, cid, tools)
    assert refusals(cid, A.send_outreach)


@pytest.mark.parametrize("kind", ["draft", "email_in", "reactivated", "interview"])
async def test_only_a_sent_email_counts_as_contact(db, kind):
    job = make_job()
    cid = make_candidate(job)
    repo.update_memory(cid, contact_log=[{"kind": kind, "ts": "2026-11-22T10:00:00.000Z", "body": "x"}])
    tools = tools_with()
    await graph.run_event(event(job, E.Tick, cid), tools=tools,
                          llm=scripted(A.send_outreach, subject="Hi", body="first real send"))
    assert len(approvals_of(cid)) == 1 and refusals(cid, A.send_outreach) == []


async def test_first_outreach_still_sends_after_approval(db):
    job = make_job()
    cid = make_candidate(job)
    aid, granted = await _granted(job, cid, A.send_outreach, {"subject": "Hello", "body": "First"})
    tools = tools_with()
    await graph.run_event(granted, tools=tools, llm=never_called_llm())
    assert [(m["subject"], m["body"]) for m in tools.email.sent] == [("Hello", "First")]
    assert len([e for e in repo.get_memory(cid).contact_log if e["kind"] == "email_out"]) == 1


async def test_replaying_the_same_approved_send_cannot_double_send(db):
    job = make_job()
    cid = make_candidate(job)
    aid, granted = await _granted(job, cid, A.send_outreach, {"subject": "Hello", "body": "Once"})
    tools = tools_with()
    await graph.run_event(granted, tools=tools, llm=never_called_llm())
    await graph.run_event(granted, tools=tools, llm=never_called_llm())
    assert len(tools.email.sent) == 1


# ---------- send_followup: only an identical recent send is refused ----------

async def test_identical_followup_is_refused(db):
    job = make_job()
    cid = make_candidate(job, S.CONTACTED)
    with_prior_send(cid, subject="Checking in", body="Any thoughts?")
    aid, granted = await _granted(job, cid, A.send_followup,
                                  {"subject": "Checking in", "body": "Any thoughts?"})
    tools = tools_with()
    await graph.run_event(granted, tools=tools, llm=never_called_llm())
    assert_nothing_sent(job, cid, tools)
    assert refusals(cid, A.send_followup)


async def test_followup_with_new_content_is_allowed_after_outreach(db):
    job = make_job()
    cid = make_candidate(job, S.CONTACTED)
    with_prior_send(cid)
    aid, granted = await _granted(job, cid, A.send_followup,
                                  {"subject": "Following up", "body": "A genuinely new message"})
    tools = tools_with()
    await graph.run_event(granted, tools=tools, llm=never_called_llm())
    assert [m["body"] for m in tools.email.sent] == ["A genuinely new message"]
    assert refusals(cid, A.send_followup) == []


# ---------- a refused outreach means they were already contacted ----------

@pytest.mark.parametrize("start", [S.OUTREACH_DRAFTED, S.APPROVED])
async def test_refused_outreach_moves_the_candidate_to_contacted(db, start):
    job = make_job()
    cid = make_candidate(job, start)
    with_prior_send(cid)
    if start == S.APPROVED:
        aid, granted = await _granted(job, cid, A.send_outreach, {"subject": "Hi", "body": "Late"})
        await graph.run_event(granted, tools=tools_with(), llm=never_called_llm())
    else:
        await graph.run_event(event(job, E.Tick, cid), tools=tools_with(),
                              llm=scripted(A.send_outreach, subject="Hi", body="again"))
    assert state_of(cid) == S.CONTACTED


async def test_refused_followup_leaves_the_state_alone(db):
    job = make_job()
    cid = make_candidate(job, S.CONTACTED)
    with_prior_send(cid, subject="Checking in", body="Any thoughts?")
    aid, granted = await _granted(job, cid, A.send_followup,
                                  {"subject": "Checking in", "body": "Any thoughts?"})
    await graph.run_event(granted, tools=tools_with(), llm=never_called_llm())
    assert state_of(cid) == S.CONTACTED
