"""Red tests for the remaining lifecycle (lane A checklist 1:40-2:50).

Covers the six unchecked items in specs/team/person-a-agent-core.md plus "Sync 2:50" (the
voice-interview path end to end). Nothing here is skipped or xfailed: every test must be collected
and must fail until the behaviour exists.

Assumed behaviour (implement to match; each block names the checklist item it covers).

1. Tick (specs/05 "Job-level events")
   - config: `settings.followup_after_hours`, `settings.stale_after_hours` (floats, demo-shortenable).
   - `dispatcher.emit_ticks()` -> list of job ids that got a job-level `Tick` (candidate_id None):
     one per ACTIVE job; none for DRAFT/PAUSED/COMPLETE; never stacks a second while one is pending.
   - A job-level Tick (`run_event`, no LLM involved) fans out one candidate-level `Tick` per CONTACTED
     candidate that is due, payload {"due": "followup" | "stale"}:
       * clock = ts of the newest `email_out` / `reactivated` entry in `memory.contact_log`;
       * an `email_in` newer than the last `email_out` means they replied -> never due;
       * exactly one `email_out` and older than followup_after_hours -> "followup" (max 1 nudge);
       * two or more `email_out` (nudge sent) and older than stale_after_hours -> "stale";
       * never a duplicate while an identical Tick for that candidate is pending.
   - A candidate Tick with payload due="followup" offers `send_followup` (not `mark_stale`); due="stale"
     offers `mark_stale` (not `send_followup`). A Tick without `due` keeps the plain state table.
   - Completion check on the same job Tick: job -> COMPLETE when SHORTLISTED >= shortlist_target, or when
     it has candidates and every one is SHORTLISTED/REJECTED/DECLINED/STALE. A PAUSED job is left alone.
   - `mark_stale` is WRITE: executes at once, CONTACTED -> STALE, no approval.

2. schedule_interview: approved -> tools.elevenlabs.create_interview(candidate_id, questions, context,
   job_id=..., candidate_name=...) -> `interviews` row (status "scheduled", conversation_id, join_url,
   questions from the approved payload) -> INTERVIEWING + an `InterviewScheduled` event
   (payload has join_url). Nothing is created before approval. In INTERVIEWING `wait` is valid.

3. InterviewDone (webhook or poller) payload {"conversation_id", "transcript"?}:
   - process_interview (READ, no approval): stores the transcript on the interviews row (status
     "completed", transcript, duration_s, completed_at), appends a contact_log entry
     {"kind": "interview", "turns": [...]}, INTERVIEWING -> INTERVIEWED, emits a follow-up InterviewDone
     so evaluate_candidate (table row: INTERVIEWED / InterviewDone) runs.
   - without a transcript in the payload it fetches tools.elevenlabs.get_transcript(conversation_id);
     if that returns None it is a `not_found` failure -> NEEDS_HUMAN.
   - evaluate_candidate: tools.evaluator.evaluate(job, memory, threshold=job.score_threshold) ->
     `evaluations` row, candidates.score, INTERVIEWED -> EVALUATED, `Evaluated` event
     (payload score + recommendation).

4. Evaluated gate (only when candidates.score is set; a NULL score keeps the plain table):
   score >= job.score_threshold -> valid has `shortlist`, not `reject_candidate_proposal`;
   below -> the reverse; borderline (latest evaluation recommendation == "borderline") -> the LLM is
   not consulted, candidate -> NEEDS_HUMAN (prev_state EVALUATED) and the Escalated payload carries
   "evaluation" {score, recommendation, summary}. reject_candidate_proposal stays approval-gated.

5. Re-plan on rejection (event ApprovalRejected, payload {approval_id, action, feedback}):
   - send_outreach rejected: redraft_outreach with the CEO feedback, up to 3 rejected drafts; the 4th
     rejection escalates without consulting the LLM or the drafter.
   - schedule_interview rejected once: re-decide (a new pending approval is allowed); rejected twice:
     escalate (NEEDS_HUMAN, prev_state INTERESTED), no new approval.
   - reject_candidate_proposal rejected (CEO override): candidate stays EVALUATED, the reject action is no
     longer offered, `shortlist` is (the override beats the score gate), and it is never re-requested.

6. HumanResolved: the candidate resumes in prev_state with that state's valid actions; the `resume`
   action (NEEDS_HUMAN -> prev_state, prev_state cleared) and `reactivate` (STALE -> CONTACTED) have
   executors; resume_candidate on a STALE candidate restarts the stale clock (a `reactivated`
   contact_log entry) so the next Tick does not immediately stale it again.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from api.agent import dispatcher, graph
from api.agent.state_machine import valid_actions
from api.config import settings
from api.contracts import (
    ActionName as A, CandidateState as S, Decision, Event, EventType as E, JobState,
)
from api.db import repo
from api.tools.base import ToolError
from api.tools.models import (
    CriterionScore, Draft, Evaluation, InterviewTranscript, ReplyClassification, Turn,
)

TERMINAL = [S.SHORTLISTED, S.REJECTED, S.DECLINED]


# ---------- fixtures / fakes ----------

@pytest.fixture
def db(tmp_path):
    repo.init_db(tmp_path / "recruiter.db")
    return repo


@pytest.fixture
def timing(monkeypatch):
    monkeypatch.setattr(settings, "followup_after_hours", 1.0)
    monkeypatch.setattr(settings, "stale_after_hours", 2.0)


def make_job(*, score_threshold=75, shortlist_target=3, **policy):
    jid = repo.create_job("Backend Eng", {}, permission_policy=policy or None,
                          score_threshold=score_threshold, shortlist_target=shortlist_target)
    repo.set_job_state(jid, JobState.ACTIVE)
    return jid


_n = iter(range(1, 10_000))


def make_candidate(job, state=S.CONTACTED, name="Ada Lovelace"):
    cid = repo.create_candidate(job, f"p{next(_n)}", name, state=state)
    repo.update_memory(cid, contact={"email": "ada@example.com", "confidence": 0.9, "source": "fake"})
    return cid


def ago(hours):
    d = datetime.now(timezone.utc) - timedelta(hours=hours)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def out(hours):
    return {"kind": "email_out", "ts": ago(hours), "to": "ada@example.com", "subject": "Hi", "body": "b"}


def inn(hours):
    return {"kind": "email_in", "ts": ago(hours), "body": "reply"}


def set_log(cid, *entries):
    repo.update_memory(cid, contact_log=list(entries))


def set_score(cid, score, recommendation=None, summary="s"):
    with repo.get_conn() as c:
        c.execute("UPDATE candidates SET score=? WHERE id=?", (score, cid))
        if recommendation:
            c.execute("INSERT INTO evaluations (id, candidate_id, job_id, score, criteria, recommendation,"
                      " summary) SELECT ?, id, job_id, ?, '[]', ?, ? FROM candidates WHERE id=?",
                      (f"ev{next(_n)}", score, recommendation, summary, cid))


def make_evaluated(job, score, recommendation):
    cid = make_candidate(job, S.EVALUATED)
    set_score(cid, score, recommendation)
    return cid


TRANSCRIPT = {
    "conversation_id": "conv1",
    "turns": [{"role": "agent", "text": "Tell me about a project."},
              {"role": "candidate", "text": "I built the ingestion pipeline."}],
    "duration_s": 300, "ended_at": "2026-11-22T10:00:00Z",
}


class FakeEmail:
    def __init__(self):
        self.sent = []

    async def send(self, to, subject, body, thread_id):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return SimpleNamespace(message_id=f"sim_{len(self.sent)}", to=to, thread_id=thread_id,
                               simulated=True)


class FakeDrafter:
    def __init__(self):
        self.feedback = []

    async def outreach(self, job, memory, feedback=None):
        self.feedback.append(feedback)
        return Draft(subject="Redraft", body="redrafted body")

    async def reply(self, job, memory, thread):
        return Draft(body="reply")


class FakeEleven:
    def __init__(self, transcript=TRANSCRIPT):
        self.calls, self.fetched, self._transcript = [], [], transcript

    async def create_interview(self, candidate_id, questions, context, **kw):
        self.calls.append({"candidate_id": candidate_id, "questions": questions,
                           "context": context, **kw})
        return SimpleNamespace(conversation_id="conv1", join_url="https://example.test/join")

    async def get_transcript(self, conversation_id):
        self.fetched.append(conversation_id)
        if self._transcript is None:
            return None
        return InterviewTranscript(**{**self._transcript, "turns": [Turn(**t) for t in
                                                                      self._transcript["turns"]]})


class FakeEvaluator:
    def __init__(self, score=91, recommendation="shortlist"):
        self.score, self.recommendation, self.calls = score, recommendation, []

    async def evaluate(self, job, memory, threshold=75):
        self.calls.append({"job": job, "memory": memory, "threshold": threshold})
        return Evaluation(
            score=self.score, recommendation=self.recommendation, summary="Strong backend depth.",
            criteria=[CriterionScore(name="Backend", weight=1.0, score=self.score,
                                     evidence=["I built the ingestion pipeline"])])


class FakePersona:
    async def classify(self, text):
        return ReplyClassification(intent="interested", reasoning="fake")

    async def reply(self, profile, thread, disposition):
        return "ok"


def tools_with(eleven=None, evaluator=None, drafter=None):
    persona = FakePersona()
    return SimpleNamespace(email=FakeEmail(), drafter=drafter or FakeDrafter(), persona=persona,
                           classifier=persona, elevenlabs=eleven or FakeEleven(),
                           evaluator=evaluator or FakeEvaluator())


def scripted(action, **args):
    calls = []

    async def llm(valid, error=None):
        calls.append(list(valid))
        return Decision(action=action, args=args, reasoning="because")

    llm.calls = calls
    return llm


def never_called_llm():
    async def llm(valid, error=None):  # pragma: no cover - failing the test is the point
        raise AssertionError("LLM must not be consulted here")
    llm.calls = []
    return llm


def event(job, type_, candidate_id=None, **payload):
    eid = repo.emit_event(job, type_, candidate_id=candidate_id, payload=payload)
    return Event(id=eid, job_id=job, candidate_id=candidate_id, type=type_, payload=payload)


def rows(sql, *args):
    with repo.get_conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def events_of(job, type_, *, candidate_id=None, after_id=0):
    return [{**r, "payload": json.loads(r["payload"] or "{}")} for r in rows(
        "SELECT * FROM events WHERE job_id=? AND type=? AND id>? ORDER BY id",
        job, type_.value, after_id) if candidate_id is None or r["candidate_id"] == candidate_id]


def candidate_ticks(job, status="pending"):
    return [r for r in events_of(job, E.Tick) if r["candidate_id"] and r["status"] == status]


def approvals_of(cid, action=None):
    return [r for r in rows("SELECT * FROM approvals WHERE candidate_id=? ORDER BY created_at, rowid", cid)
            if action is None or r["action"] == action.value]


def state_of(cid):
    return S(repo.get_candidate(cid)["state"])


def interviews_of(cid):
    return rows("SELECT * FROM interviews WHERE candidate_id=?", cid)


def evaluations_of(cid):
    return rows("SELECT * FROM evaluations WHERE candidate_id=? ORDER BY created_at, rowid", cid)


async def job_tick(job, tools=None):
    await graph.run_event(event(job, E.Tick), tools=tools or tools_with(), llm=never_called_llm())


def reject_approval(job, cid, action, payload, feedback="no"):
    aid = repo.create_approval(job, cid, action, payload, "r")
    repo.decide_approval(aid, False, feedback=feedback)
    return aid


# =====================================================================
# 1. Tick: follow-up, stale, completion
# =====================================================================

def test_emit_ticks_one_job_level_tick_per_active_job_only(db):
    active, paused, draft, done = make_job(), make_job(), make_job(), make_job()
    repo.set_job_state(paused, JobState.PAUSED)
    repo.set_job_state(draft, JobState.DRAFT)
    repo.set_job_state(done, JobState.COMPLETE)
    assert dispatcher.emit_ticks() == [active]
    for j, n in [(active, 1), (paused, 0), (draft, 0), (done, 0)]:
        assert len([r for r in events_of(j, E.Tick) if r["candidate_id"] is None]) == n


def test_emit_ticks_does_not_stack_while_one_is_pending(db):
    job = make_job()
    dispatcher.emit_ticks()
    assert dispatcher.emit_ticks() == []
    assert len(events_of(job, E.Tick)) == 1
    repo.mark_event_done(events_of(job, E.Tick)[0]["id"])
    assert dispatcher.emit_ticks() == [job]


async def test_job_tick_flags_a_silent_candidate_for_a_followup(db, timing):
    job = make_job()
    cid = make_candidate(job)
    set_log(cid, out(1.5))
    await job_tick(job)
    (t,) = candidate_ticks(job)
    assert t["candidate_id"] == cid and t["payload"]["due"] == "followup"


async def test_job_tick_leaves_a_fresh_send_alone(db, timing):
    job = make_job()
    cid = make_candidate(job)
    set_log(cid, out(0.1))
    await job_tick(job)
    assert candidate_ticks(job) == []


async def test_job_tick_skips_a_candidate_who_replied(db, timing):
    job = make_job()
    cid = make_candidate(job)
    set_log(cid, out(5), inn(4))
    await job_tick(job)
    assert candidate_ticks(job) == []


async def test_job_tick_allows_at_most_one_nudge(db, timing):
    job = make_job()
    cid = make_candidate(job)
    set_log(cid, out(10), out(0.5))  # nudged recently: not due for anything yet
    await job_tick(job)
    assert candidate_ticks(job) == []


async def test_job_tick_flags_stale_after_the_nudge_goes_unanswered(db, timing):
    job = make_job()
    cid = make_candidate(job)
    set_log(cid, out(10), out(3))
    await job_tick(job)
    (t,) = candidate_ticks(job)
    assert t["candidate_id"] == cid and t["payload"]["due"] == "stale"


async def test_job_tick_only_considers_contacted_candidates(db, timing):
    job = make_job()
    for st in (S.OUTREACH_DRAFTED, S.INTERESTED, S.INTERVIEWING, S.STALE, S.NEEDS_HUMAN):
        cid = make_candidate(job, st, name=f"C {st.value}")
        set_log(cid, out(9))
    await job_tick(job)
    assert candidate_ticks(job) == []


async def test_job_tick_does_not_duplicate_a_pending_candidate_tick(db, timing):
    job = make_job()
    cid = make_candidate(job)
    set_log(cid, out(1.5))
    await job_tick(job)
    await job_tick(job)
    assert len(candidate_ticks(job)) == 1


async def test_job_tick_is_ignored_for_a_paused_job(db, timing):
    job = make_job()
    cid = make_candidate(job)
    set_log(cid, out(1.5))
    repo.set_job_state(job, JobState.PAUSED)
    await job_tick(job)
    assert candidate_ticks(job) == []
    assert repo.get_job(job)["state"] == JobState.PAUSED.value


async def test_followup_tick_offers_send_followup_but_not_mark_stale(db, timing):
    job = make_job()
    cid = make_candidate(job)
    set_log(cid, out(1.5))
    llm = scripted(A.send_followup, subject="Re: Hi", body="nudge")
    await graph.run_event(event(job, E.Tick, cid, due="followup"), tools=tools_with(), llm=llm)
    assert A.send_followup in llm.calls[0] and A.mark_stale not in llm.calls[0]
    (ap,) = approvals_of(cid, A.send_followup)  # RESTRICTED: waits for the CEO
    assert ap["status"] == "pending" and state_of(cid) == S.CONTACTED


async def test_stale_tick_offers_mark_stale_but_not_send_followup(db, timing):
    job = make_job()
    cid = make_candidate(job)
    set_log(cid, out(10), out(3))
    llm = scripted(A.mark_stale)
    await graph.run_event(event(job, E.Tick, cid, due="stale"), tools=tools_with(), llm=llm)
    assert A.mark_stale in llm.calls[0] and A.send_followup not in llm.calls[0]


async def test_mark_stale_executes_without_approval(db, timing):
    job = make_job()
    cid = make_candidate(job)
    set_log(cid, out(10), out(3))
    await graph.run_event(event(job, E.Tick, cid, due="stale"), tools=tools_with(),
                          llm=scripted(A.mark_stale))
    assert state_of(cid) == S.STALE
    assert approvals_of(cid) == []
    assert any(d["action"] == "mark_stale" for d in repo.get_memory(cid).decisions)


async def test_full_silence_path_followup_then_stale(db, timing):
    job = make_job(auto_approve=True)
    cid = make_candidate(job)
    tools = tools_with()
    set_log(cid, out(1.5))
    await graph.run_event(event(job, E.Tick, cid, due="followup"), tools=tools,
                          llm=scripted(A.send_followup, subject="Re: Hi", body="nudge"))
    assert len(tools.email.sent) == 1 and state_of(cid) == S.CONTACTED
    log = repo.get_memory(cid).contact_log
    assert [e["kind"] for e in log].count("email_out") == 2
    # the nudge is fresh, so nothing is due right now ...
    await job_tick(job)
    assert candidate_ticks(job) == []
    # ... and after M hours of silence the next job Tick marks it for staleness
    set_log(cid, out(10), out(3))
    await job_tick(job)
    assert [t["payload"]["due"] for t in candidate_ticks(job)] == ["stale"]


async def test_job_completes_when_the_shortlist_target_is_reached(db):
    job = make_job(shortlist_target=2)
    make_candidate(job, S.SHORTLISTED, name="A One")
    make_candidate(job, S.SHORTLISTED, name="B Two")
    make_candidate(job, S.CONTACTED, name="C Three")
    await job_tick(job)
    assert repo.get_job(job)["state"] == JobState.COMPLETE.value


async def test_job_completes_when_every_candidate_is_terminal(db):
    job = make_job(shortlist_target=5)
    for st, n in [(S.SHORTLISTED, "A One"), (S.REJECTED, "B Two"), (S.DECLINED, "C Three"),
                  (S.STALE, "D Four")]:
        make_candidate(job, st, name=n)
    await job_tick(job)
    assert repo.get_job(job)["state"] == JobState.COMPLETE.value


async def test_job_stays_active_while_a_candidate_is_in_flight(db):
    job = make_job(shortlist_target=5)
    make_candidate(job, S.SHORTLISTED, name="A One")
    make_candidate(job, S.NEEDS_HUMAN, name="B Two")  # a human still has to act
    await job_tick(job)
    assert repo.get_job(job)["state"] == JobState.ACTIVE.value


async def test_job_without_candidates_does_not_complete(db):
    job = make_job()
    await job_tick(job)
    assert repo.get_job(job)["state"] == JobState.ACTIVE.value


async def test_paused_job_never_completes(db):
    job = make_job(shortlist_target=1)
    make_candidate(job, S.SHORTLISTED)
    repo.set_job_state(job, JobState.PAUSED)
    await job_tick(job)
    assert repo.get_job(job)["state"] == JobState.PAUSED.value


# =====================================================================
# 2. schedule_interview -> interviews row -> INTERVIEWING
# =====================================================================

QUESTIONS = ["Walk me through your last backend project.", "How do you handle backpressure?"]


def approve_interview(job, cid, questions=QUESTIONS):
    aid = repo.create_approval(job, cid, A.schedule_interview,
                               {"questions": questions, "context": "backend role"}, "good fit")
    repo.decide_approval(aid, True)
    return aid


async def test_approved_schedule_interview_creates_session_and_interviews_row(db):
    job = make_job()
    cid = make_candidate(job, S.INTERESTED)
    tools = tools_with()
    aid = approve_interview(job, cid)
    await graph.run_event(event(job, E.ApprovalGranted, cid, approval_id=aid,
                                action="schedule_interview"),
                          tools=tools, llm=never_called_llm())
    (call,) = tools.elevenlabs.calls
    assert call["candidate_id"] == cid and call["questions"] == QUESTIONS
    assert call["job_id"] == job
    (iv,) = interviews_of(cid)
    assert iv["job_id"] == job and iv["status"] == "scheduled"
    assert iv["conversation_id"] == "conv1" and iv["join_url"] == "https://example.test/join"
    assert json.loads(iv["questions"]) == QUESTIONS
    assert state_of(cid) == S.INTERVIEWING
    assert repo.get_approval(aid).status == "executed"


async def test_schedule_interview_emits_interview_scheduled_with_the_join_link(db):
    job = make_job()
    cid = make_candidate(job, S.INTERESTED)
    aid = approve_interview(job, cid)
    await graph.run_event(event(job, E.ApprovalGranted, cid, approval_id=aid),
                          tools=tools_with(), llm=never_called_llm())
    (ev,) = events_of(job, E.InterviewScheduled, candidate_id=cid)
    assert ev["payload"]["join_url"] == "https://example.test/join"


async def test_schedule_interview_creates_nothing_before_approval(db):
    job = make_job()
    cid = make_candidate(job, S.INTERESTED)
    tools = tools_with()
    await graph.run_event(event(job, E.EmailReplied, cid, body="yes please"), tools=tools,
                          llm=scripted(A.schedule_interview, questions=QUESTIONS))
    assert tools.elevenlabs.calls == [] and interviews_of(cid) == []
    assert state_of(cid) == S.INTERESTED
    assert approvals_of(cid, A.schedule_interview)[0]["status"] == "pending"


async def test_auto_approved_schedule_interview_runs_in_one_pass(db):
    job = make_job(auto_approve=True)
    cid = make_candidate(job, S.INTERESTED)
    tools = tools_with()
    await graph.run_event(event(job, E.EmailReplied, cid, body="yes"), tools=tools,
                          llm=scripted(A.schedule_interview, questions=QUESTIONS))
    assert len(tools.elevenlabs.calls) == 1
    assert len(interviews_of(cid)) == 1 and state_of(cid) == S.INTERVIEWING


async def test_interviewing_candidate_waits_on_interview_scheduled(db):
    job = make_job()
    cid = make_candidate(job, S.INTERVIEWING)
    llm = scripted(A.wait)
    await graph.run_event(event(job, E.InterviewScheduled, cid, join_url="u"),
                          tools=tools_with(), llm=llm)
    assert A.wait in llm.calls[0] and state_of(cid) == S.INTERVIEWING


# =====================================================================
# 3. InterviewDone -> process_interview -> evaluate_candidate
# =====================================================================

def interviewing(job):
    cid = make_candidate(job, S.INTERVIEWING)
    with repo.get_conn() as c:
        c.execute("INSERT INTO interviews (id, candidate_id, job_id, conversation_id, join_url, questions,"
                  " status) VALUES (?,?,?,?,?,?,?)",
                  (f"iv{next(_n)}", cid, job, "conv1", "https://example.test/join",
                   json.dumps(QUESTIONS), "scheduled"))
    return cid


async def test_process_interview_stores_the_transcript_and_moves_to_interviewed(db):
    job = make_job()
    cid = interviewing(job)
    await graph.run_event(event(job, E.InterviewDone, cid, conversation_id="conv1",
                                transcript=TRANSCRIPT),
                          tools=tools_with(), llm=scripted(A.process_interview))
    assert state_of(cid) == S.INTERVIEWED
    (iv,) = interviews_of(cid)
    assert iv["status"] == "completed" and iv["duration_s"] == 300 and iv["completed_at"]
    assert "I built the ingestion pipeline." in iv["transcript"]
    (entry,) = [e for e in repo.get_memory(cid).contact_log if e["kind"] == "interview"]
    assert [t["text"] for t in entry["turns"]][-1] == "I built the ingestion pipeline."


async def test_process_interview_needs_no_approval(db):
    job = make_job()
    cid = interviewing(job)
    await graph.run_event(event(job, E.InterviewDone, cid, conversation_id="conv1",
                                transcript=TRANSCRIPT),
                          tools=tools_with(), llm=scripted(A.process_interview))
    assert state_of(cid) == S.INTERVIEWED and approvals_of(cid) == []


async def test_process_interview_queues_the_evaluation_step(db):
    job = make_job()
    cid = interviewing(job)
    ev = event(job, E.InterviewDone, cid, conversation_id="conv1", transcript=TRANSCRIPT)
    await graph.run_event(ev, tools=tools_with(), llm=scripted(A.process_interview))
    follow_ups = events_of(job, E.InterviewDone, candidate_id=cid, after_id=ev.id)
    assert len(follow_ups) == 1 and follow_ups[0]["status"] == "pending"


async def test_process_interview_fetches_the_transcript_when_the_event_has_none(db):
    job = make_job()
    cid = interviewing(job)
    tools = tools_with()
    await graph.run_event(event(job, E.InterviewDone, cid, conversation_id="conv1"),
                          tools=tools, llm=scripted(A.process_interview))
    assert tools.elevenlabs.fetched == ["conv1"]
    assert state_of(cid) == S.INTERVIEWED
    assert "I built the ingestion pipeline." in interviews_of(cid)[0]["transcript"]


async def test_process_interview_without_any_transcript_escalates(db):
    job = make_job()
    cid = interviewing(job)
    await graph.run_event(event(job, E.InterviewDone, cid, conversation_id="conv1"),
                          tools=tools_with(eleven=FakeEleven(transcript=None)),
                          llm=scripted(A.process_interview))
    assert state_of(cid) == S.NEEDS_HUMAN
    assert repo.get_candidate(cid)["prev_state"] == S.INTERVIEWING.value
    assert interviews_of(cid)[0]["status"] == "scheduled"
    (failed,) = events_of(job, E.ToolFailed, candidate_id=cid)  # a real not_found, not "no executor"
    assert failed["payload"]["kind"] == "not_found" and failed["payload"]["action"] == "process_interview"


async def test_evaluate_candidate_writes_the_evaluation_row_and_score(db):
    job = make_job(score_threshold=80)
    cid = interviewing(job)
    ev_tool = FakeEvaluator(score=91, recommendation="shortlist")
    tools = tools_with(evaluator=ev_tool)
    ev = event(job, E.InterviewDone, cid, conversation_id="conv1", transcript=TRANSCRIPT)
    await graph.run_event(ev, tools=tools, llm=scripted(A.process_interview))
    (nxt,) = [e for e in repo.fetch_due_events(10, job)
              if e.type == E.InterviewDone and e.candidate_id == cid and e.id > ev.id]
    await graph.run_event(nxt, tools=tools, llm=scripted(A.evaluate_candidate))
    assert state_of(cid) == S.EVALUATED
    assert repo.get_candidate(cid)["score"] == 91
    (row,) = evaluations_of(cid)
    assert row["score"] == 91 and row["recommendation"] == "shortlist"
    assert row["summary"] == "Strong backend depth."
    assert json.loads(row["criteria"])[0]["evidence"] == ["I built the ingestion pipeline"]
    (call,) = ev_tool.calls
    assert call["threshold"] == 80  # job.score_threshold, not the evaluator default
    assert "I built the ingestion pipeline." in json.dumps(call["memory"].model_dump())
    (done,) = events_of(job, E.Evaluated, candidate_id=cid)
    assert done["payload"]["score"] == 91 and done["payload"]["recommendation"] == "shortlist"


async def test_evaluate_candidate_is_offered_in_interviewed_without_approval(db):
    job = make_job()
    cid = make_candidate(job, S.INTERVIEWED)
    tools = tools_with()
    llm = scripted(A.evaluate_candidate)
    await graph.run_event(event(job, E.InterviewDone, cid), tools=tools, llm=llm)
    assert A.evaluate_candidate in llm.calls[0]
    assert state_of(cid) == S.EVALUATED and approvals_of(cid) == []
    assert len(tools.evaluator.calls) == 1


# =====================================================================
# 4. Evaluated: shortlist / reject / borderline
# =====================================================================

async def test_high_score_offers_shortlist_not_reject(db):
    job = make_job(score_threshold=75)
    cid = make_evaluated(job, 90, "shortlist")
    llm = scripted(A.shortlist)
    await graph.run_event(event(job, E.Evaluated, cid, score=90), tools=tools_with(), llm=llm)
    assert A.shortlist in llm.calls[0] and A.reject_candidate_proposal not in llm.calls[0]


async def test_shortlist_is_applied_without_approval(db):
    job = make_job(score_threshold=75)
    cid = make_evaluated(job, 90, "shortlist")
    await graph.run_event(event(job, E.Evaluated, cid, score=90), tools=tools_with(),
                          llm=scripted(A.shortlist))
    assert state_of(cid) == S.SHORTLISTED and approvals_of(cid) == []


async def test_score_at_the_threshold_counts_as_shortlist(db):
    job = make_job(score_threshold=75)
    cid = make_evaluated(job, 75, "shortlist")
    llm = scripted(A.shortlist)
    await graph.run_event(event(job, E.Evaluated, cid, score=75), tools=tools_with(), llm=llm)
    assert A.shortlist in llm.calls[0] and A.reject_candidate_proposal not in llm.calls[0]


async def test_low_score_offers_reject_not_shortlist(db):
    job = make_job(score_threshold=75)
    cid = make_evaluated(job, 40, "reject")
    llm = scripted(A.reject_candidate_proposal, reason="below the bar")
    await graph.run_event(event(job, E.Evaluated, cid, score=40), tools=tools_with(), llm=llm)
    assert A.reject_candidate_proposal in llm.calls[0] and A.shortlist not in llm.calls[0]


async def test_rejection_waits_for_the_ceo_then_rejects(db):
    job = make_job(score_threshold=75, auto_approve=True)  # blanket auto-approve never covers rejection
    cid = make_evaluated(job, 40, "reject")
    await graph.run_event(event(job, E.Evaluated, cid, score=40), tools=tools_with(),
                          llm=scripted(A.reject_candidate_proposal, reason="below the bar"))
    (ap,) = approvals_of(cid, A.reject_candidate_proposal)
    assert ap["status"] == "pending" and state_of(cid) == S.EVALUATED
    repo.decide_approval(ap["id"], True)
    await graph.run_event(event(job, E.ApprovalGranted, cid, approval_id=ap["id"]),
                          tools=tools_with(), llm=never_called_llm())
    assert state_of(cid) == S.REJECTED
    assert repo.get_approval(ap["id"]).status == "executed"


async def test_borderline_goes_to_the_ceo_with_the_evaluation_attached(db):
    job = make_job(score_threshold=75)
    cid = make_evaluated(job, 70, "borderline")
    await graph.run_event(event(job, E.Evaluated, cid, score=70, recommendation="borderline"),
                          tools=tools_with(), llm=never_called_llm())
    assert state_of(cid) == S.NEEDS_HUMAN
    assert repo.get_candidate(cid)["prev_state"] == S.EVALUATED.value
    (esc,) = events_of(job, E.Escalated, candidate_id=cid)
    ev = esc["payload"]["evaluation"]
    assert ev["score"] == 70 and ev["recommendation"] == "borderline" and ev["summary"]
    assert approvals_of(cid) == []


async def test_clear_shortlist_and_clear_reject_are_never_escalated(db):
    job = make_job(score_threshold=75)
    hi, lo = make_evaluated(job, 90, "shortlist"), make_evaluated(job, 40, "reject")
    for cid, act in [(hi, A.shortlist), (lo, A.reject_candidate_proposal)]:
        await graph.run_event(event(job, E.Evaluated, cid), tools=tools_with(), llm=scripted(act))
        assert state_of(cid) != S.NEEDS_HUMAN


# =====================================================================
# 5. Re-plan on rejection
# =====================================================================

@pytest.mark.parametrize("n_rejected", [1, 2, 3])
async def test_rejected_draft_is_redrafted_with_the_ceo_feedback(db, n_rejected):
    job = make_job()
    cid = make_candidate(job, S.OUTREACH_DRAFTED)
    for _ in range(n_rejected):
        aid = reject_approval(job, cid, A.send_outreach, {"subject": "s", "body": "b"}, "too formal")
    drafter = FakeDrafter()
    llm = scripted(A.redraft_outreach)
    ev = event(job, E.ApprovalRejected, cid, approval_id=aid, action="send_outreach",
               feedback="too formal")
    await graph.run_event(ev, tools=tools_with(drafter=drafter), llm=llm)
    assert A.redraft_outreach in llm.calls[0]
    assert drafter.feedback == ["too formal"]
    assert state_of(cid) == S.OUTREACH_DRAFTED
    assert len(events_of(job, E.OutreachDrafted, candidate_id=cid, after_id=ev.id)) == 1


async def test_fourth_rejected_draft_escalates_instead_of_redrafting(db):
    job = make_job()
    cid = make_candidate(job, S.OUTREACH_DRAFTED)
    for _ in range(4):
        aid = reject_approval(job, cid, A.send_outreach, {"subject": "s", "body": "b"}, "still bad")
    drafter = FakeDrafter()
    await graph.run_event(event(job, E.ApprovalRejected, cid, approval_id=aid, action="send_outreach",
                                feedback="still bad"),
                          tools=tools_with(drafter=drafter), llm=never_called_llm())
    assert drafter.feedback == []
    assert state_of(cid) == S.NEEDS_HUMAN
    assert repo.get_candidate(cid)["prev_state"] == S.OUTREACH_DRAFTED.value


async def test_first_interview_rejection_re_decides_and_may_request_again(db):
    job = make_job()
    cid = make_candidate(job, S.INTERESTED)
    aid = reject_approval(job, cid, A.schedule_interview, {"questions": QUESTIONS}, "ask about Kafka")
    llm = scripted(A.schedule_interview, questions=["Tell me about Kafka."])
    await graph.run_event(event(job, E.ApprovalRejected, cid, approval_id=aid,
                                action="schedule_interview", feedback="ask about Kafka"),
                          tools=tools_with(), llm=llm)
    assert A.schedule_interview in llm.calls[0]
    pending = [a for a in approvals_of(cid, A.schedule_interview) if a["status"] == "pending"]
    assert len(pending) == 1 and json.loads(pending[0]["payload"])["questions"] == ["Tell me about Kafka."]
    assert state_of(cid) == S.INTERESTED


async def test_interview_rejected_twice_escalates(db):
    job = make_job()
    cid = make_candidate(job, S.INTERESTED)
    reject_approval(job, cid, A.schedule_interview, {"questions": QUESTIONS}, "no")
    aid = reject_approval(job, cid, A.schedule_interview, {"questions": ["q"]}, "still no")
    await graph.run_event(event(job, E.ApprovalRejected, cid, approval_id=aid,
                                action="schedule_interview", feedback="still no"),
                          tools=tools_with(), llm=never_called_llm())
    assert state_of(cid) == S.NEEDS_HUMAN
    assert repo.get_candidate(cid)["prev_state"] == S.INTERESTED.value
    assert [a["status"] for a in approvals_of(cid, A.schedule_interview)] == ["rejected", "rejected"]


async def test_rejection_override_keeps_the_candidate_evaluated(db):
    job = make_job(score_threshold=75)
    cid = make_evaluated(job, 40, "reject")
    aid = reject_approval(job, cid, A.reject_candidate_proposal, {"reason": "low"}, "give them a chance")
    llm = scripted(A.shortlist)
    await graph.run_event(event(job, E.ApprovalRejected, cid, approval_id=aid,
                                action="reject_candidate_proposal", feedback="give them a chance"),
                          tools=tools_with(), llm=llm)
    assert A.reject_candidate_proposal not in llm.calls[0]  # no re-asking after an override
    assert A.shortlist in llm.calls[0]  # the CEO's decision beats the score gate
    assert len(approvals_of(cid, A.reject_candidate_proposal)) == 1
    assert state_of(cid) == S.SHORTLISTED


async def test_rejection_override_never_rejects_the_candidate(db):
    job = make_job(score_threshold=75)
    cid = make_evaluated(job, 40, "reject")
    aid = reject_approval(job, cid, A.reject_candidate_proposal, {"reason": "low"})
    await graph.run_event(event(job, E.ApprovalRejected, cid, approval_id=aid,
                                action="reject_candidate_proposal"),
                          tools=tools_with(), llm=scripted(A.shortlist))
    assert state_of(cid) == S.SHORTLISTED  # the override lets the shortlist through, never a reject


# =====================================================================
# 6. HumanResolved: resume, reactivate
# =====================================================================

RESUMABLE = [S.OUTREACH_DRAFTED, S.CONTACTED, S.INTERESTED, S.INTERVIEWING, S.INTERVIEWED]


@pytest.mark.parametrize("prev", RESUMABLE)
async def test_human_resolved_resumes_with_the_previous_states_actions(db, prev):
    job = make_job()
    cid = make_candidate(job, prev)
    assert repo.escalate_candidate(cid, "boom", trace_url="https://trace.test/1")
    repo.resume_candidate(cid)
    assert state_of(cid) == prev
    llm = scripted(A.wait if A.wait in valid_actions(prev) else A.escalate)
    await graph.run_event(event(job, E.HumanResolved, cid), tools=tools_with(), llm=llm)
    assert set(llm.calls[0]) == set(valid_actions(prev))


async def test_resume_action_returns_a_needs_human_candidate_to_prev_state(db):
    job = make_job()
    cid = make_candidate(job, S.INTERVIEWING)
    repo.escalate_candidate(cid, "boom")
    llm = scripted(A.resume)
    await graph.run_event(event(job, E.HumanResolved, cid), tools=tools_with(), llm=llm)
    assert llm.calls[0] == [A.resume]
    assert state_of(cid) == S.INTERVIEWING
    assert repo.get_candidate(cid)["prev_state"] is None


async def test_reactivate_action_moves_stale_back_to_contacted(db):
    job = make_job()
    cid = make_candidate(job, S.STALE)
    llm = scripted(A.reactivate)
    await graph.run_event(event(job, E.HumanResolved, cid), tools=tools_with(), llm=llm)
    assert A.reactivate in llm.calls[0]
    assert state_of(cid) == S.CONTACTED


async def test_reactivating_a_stale_candidate_restarts_the_stale_clock(db, timing):
    job = make_job()
    cid = make_candidate(job, S.STALE)
    set_log(cid, out(30), out(25))  # long dead
    repo.resume_candidate(cid)
    assert state_of(cid) == S.CONTACTED
    assert any(e["kind"] == "reactivated" for e in repo.get_memory(cid).contact_log)
    await job_tick(job)
    assert candidate_ticks(job) == []  # not instantly stale again


async def test_reactivated_candidate_can_go_stale_again_after_m_hours(db, timing):
    job = make_job()
    cid = make_candidate(job, S.CONTACTED)
    set_log(cid, out(30), out(25), {"kind": "reactivated", "ts": ago(3)})
    await job_tick(job)
    assert [t["payload"]["due"] for t in candidate_ticks(job)] == ["stale"]


# =====================================================================
# Sync 2:50: the voice-interview path, end to end
# =====================================================================

async def drain(job, tools, llm, rounds=40):
    for _ in range(rounds):
        due = repo.fetch_due_events(50, job)
        if not due:
            return
        for e in due:
            if repo.claim_event(e.id):
                await graph.run_event(e, tools=tools, llm=llm)
                repo.mark_event_done(e.id)
    raise AssertionError("events never settled")


async def test_voice_interview_path_end_to_end(db):
    job = make_job(score_threshold=75, shortlist_target=1, auto_approve=True)
    cid = make_candidate(job, S.INTERESTED)
    tools = tools_with(evaluator=FakeEvaluator(score=91, recommendation="shortlist"))
    flags = {"interview_done": False}

    async def llm(valid, error=None):
        if A.schedule_interview in valid:
            return Decision(action=A.schedule_interview, args={"questions": QUESTIONS},
                            reasoning="interested, schedule it")
        if A.wait in valid and not flags["interview_done"]:
            return Decision(action=A.wait, args={}, reasoning="link is live")
        for act in (A.process_interview, A.evaluate_candidate, A.shortlist):
            if act in valid:
                return Decision(action=act, args={}, reasoning="next step")
        raise AssertionError(f"nothing sensible to pick from {valid}")

    repo.emit_event(job, E.EmailReplied, cid, {"body": "Yes, happy to interview."})
    await drain(job, tools, llm)
    assert state_of(cid) == S.INTERVIEWING
    assert len(interviews_of(cid)) == 1 and interviews_of(cid)[0]["status"] == "scheduled"

    # the ElevenLabs post-call webhook lands
    flags["interview_done"] = True
    repo.emit_event(job, E.InterviewDone, cid, {"conversation_id": "conv1", "transcript": TRANSCRIPT})
    await drain(job, tools, llm)

    assert state_of(cid) == S.SHORTLISTED
    assert repo.get_candidate(cid)["score"] == 91
    assert interviews_of(cid)[0]["status"] == "completed"
    assert len(evaluations_of(cid)) == 1 and len(tools.evaluator.calls) == 1
    assert len(tools.elevenlabs.calls) == 1
    assert events_of(job, E.ToolFailed) == [] and events_of(job, E.Escalated) == []
    assert all(a["status"] == "executed" for a in approvals_of(cid))

    await job_tick(job)  # shortlist target of 1 reached
    assert repo.get_job(job)["state"] == JobState.COMPLETE.value
