"""Red tests for api/agent/dispatcher.py (lane A, core loop).

Source: specs/05-agent-graph.md "Event dispatcher"; checklist item
"1s poll loop, respects not_before, serial per candidate, Semaphore(MAX_CONCURRENT_RUNS),
skips PAUSED jobs" + crash recovery.

Assumed API (implement to match):
    Dispatcher(handler, *, max_concurrent=4, poll_interval=1.0, batch_size=10,
               job_id=None, max_attempts=3, on_exhausted=None, owner=None)
        handler:      async (Event) -> None   (the graph invocation)
        on_exhausted: async (Event, Exception) -> None, called once when an event's
                      attempts run out (the escalate hook)
        job_id:       restrict to one job (the JOB_ID env mode)
    await Dispatcher.run_once() -> int
        one poll cycle: fetch due events, process them, and return when they finish.
        Returns how many events it attempted.
        - claims each event atomically (pending -> processing), marks done / failed
        - events of one candidate run serially in id order; a candidate's later events
          wait behind an earlier one that failed or is already in flight
        - job-level events (candidate_id None) are serial per job
        - different candidates run concurrently, bounded by max_concurrent
        - holds the per-candidate lock while the handler runs; if another owner holds
          it, the event stays pending and is not counted as a failed attempt
    start_dispatcher(handler=None, **Dispatcher kwargs) -> None   (sync; needs a running loop)
        runs startup recovery (reset_stuck_events + release_stale_locks), then polls in a
        background task. A second call while running is a no-op.
    stop_dispatcher() -> None   (sync, idempotent, cancels the task)
"""
import asyncio

import pytest

from api.contracts import EventType as E, JobState
from api.db import repo
from api.agent.dispatcher import Dispatcher, start_dispatcher, stop_dispatcher


@pytest.fixture
def db(tmp_path):
    repo.init_db(tmp_path / "recruiter.db")
    yield repo
    stop_dispatcher()


@pytest.fixture
def job(db):
    jid = db.create_job("Role", {})
    db.set_job_state(jid, JobState.ACTIVE)
    return jid


def mk_cands(db, job, n):
    return [db.create_candidate(job, f"p-{i}", f"Cand {i}") for i in range(n)]


def status_of(eid):
    with repo.get_conn() as c:
        return c.execute("SELECT status, attempts FROM events WHERE id=?", (eid,)).fetchone()


class Recorder:
    """Async handler that records order and overlap."""

    def __init__(self, delay=0.0, fail_ids=()):
        self.delay, self.fail_ids = delay, set(fail_ids)
        self.seen: list[int] = []
        self.events = {}
        self.active = 0
        self.max_active = 0
        self.active_by_cand: dict = {}
        self.max_by_cand: dict = {}

    async def __call__(self, event):
        self.seen.append(event.id)
        self.events[event.id] = event
        k = event.candidate_id or f"job:{event.job_id}"
        self.active += 1
        self.active_by_cand[k] = self.active_by_cand.get(k, 0) + 1
        self.max_active = max(self.max_active, self.active)
        self.max_by_cand[k] = max(self.max_by_cand.get(k, 0), self.active_by_cand[k])
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if event.id in self.fail_ids:
                raise RuntimeError(f"boom {event.id}")
        finally:
            self.active -= 1
            self.active_by_cand[k] -= 1


# ---------- basic processing ----------

async def test_run_once_processes_pending_event_and_marks_done(db, job):
    (cid,) = mk_cands(db, job, 1)
    eid = db.emit_event(job, E.CandidateFound, candidate_id=cid, payload={"x": 1})
    h = Recorder()
    assert await Dispatcher(h).run_once() == 1
    assert h.seen == [eid]
    assert h.events[eid].payload == {"x": 1} and h.events[eid].candidate_id == cid
    assert tuple(status_of(eid)) == ("done", 0)


async def test_run_once_with_nothing_due_returns_zero(db, job):
    assert await Dispatcher(Recorder()).run_once() == 0


async def test_done_events_are_not_processed_twice(db, job):
    (cid,) = mk_cands(db, job, 1)
    db.emit_event(job, E.Tick, candidate_id=cid)
    h = Recorder()
    d = Dispatcher(h)
    await d.run_once()
    assert await d.run_once() == 0
    assert len(h.seen) == 1


async def test_event_is_processing_while_handler_runs(db, job):
    (cid,) = mk_cands(db, job, 1)
    eid = db.emit_event(job, E.Tick, candidate_id=cid)
    seen = []

    async def h(event):
        seen.append(status_of(event.id)["status"])

    await Dispatcher(h).run_once()
    assert seen == ["processing"]


async def test_job_level_events_are_processed(db, job):
    eid = db.emit_event(job, E.JobCreated)
    h = Recorder()
    await Dispatcher(h).run_once()
    assert h.seen == [eid] and h.events[eid].candidate_id is None


# ---------- scheduling rules ----------

async def test_respects_not_before(db, job):
    (cid,) = mk_cands(db, job, 1)
    future = db.emit_event(job, E.EmailReplied, candidate_id=cid, not_before="2999-01-01T00:00:00.000Z")
    due = db.emit_event(job, E.Tick, candidate_id=cid, not_before="2000-01-01T00:00:00.000Z")
    h = Recorder()
    await Dispatcher(h).run_once()
    assert h.seen == [due]
    assert status_of(future)["status"] == "pending"


async def test_paused_job_events_are_skipped_then_resume(db, job):
    (cid,) = mk_cands(db, job, 1)
    eid = db.emit_event(job, E.Tick, candidate_id=cid)
    db.set_job_state(job, JobState.PAUSED)
    h = Recorder()
    d = Dispatcher(h)
    assert await d.run_once() == 0
    assert status_of(eid)["status"] == "pending"
    db.set_job_state(job, JobState.ACTIVE)
    assert await d.run_once() == 1
    assert h.seen == [eid]


async def test_other_jobs_keep_running_while_one_is_paused(db, job):
    other = db.create_job("Other", {})
    db.set_job_state(other, JobState.ACTIVE)
    (c1,) = mk_cands(db, job, 1)
    c2 = db.create_candidate(other, "p-z", "Z")
    paused = db.emit_event(job, E.Tick, candidate_id=c1)
    live = db.emit_event(other, E.Tick, candidate_id=c2)
    db.set_job_state(job, JobState.PAUSED)
    h = Recorder()
    await Dispatcher(h).run_once()
    assert h.seen == [live]
    assert status_of(paused)["status"] == "pending"


async def test_job_id_restriction(db, job):
    other = db.create_job("Other", {})
    db.set_job_state(other, JobState.ACTIVE)
    (c1,) = mk_cands(db, job, 1)
    c2 = db.create_candidate(other, "p-z", "Z")
    mine = db.emit_event(job, E.Tick, candidate_id=c1)
    theirs = db.emit_event(other, E.Tick, candidate_id=c2)
    h = Recorder()
    await Dispatcher(h, job_id=job).run_once()
    assert h.seen == [mine]
    assert status_of(theirs)["status"] == "pending"


async def test_batch_size_limits_one_cycle(db, job):
    cands = mk_cands(db, job, 15)
    ids = [db.emit_event(job, E.Tick, candidate_id=c) for c in cands]
    h = Recorder()
    d = Dispatcher(h, batch_size=10)
    assert await d.run_once() == 10
    assert sorted(h.seen) == ids[:10]
    assert await d.run_once() == 5


# ---------- serial per candidate, concurrent across candidates ----------

async def test_same_candidate_events_run_serially_in_id_order(db, job):
    (cid,) = mk_cands(db, job, 1)
    ids = [db.emit_event(job, E.Tick, candidate_id=cid) for _ in range(4)]
    h = Recorder(delay=0.02)
    assert await Dispatcher(h).run_once() == 4
    assert h.seen == ids
    assert h.max_by_cand[cid] == 1


async def test_different_candidates_run_concurrently(db, job):
    cands = mk_cands(db, job, 3)
    for c in cands:
        db.emit_event(job, E.Tick, candidate_id=c)
    h = Recorder(delay=0.05)
    await Dispatcher(h, max_concurrent=4).run_once()
    assert h.max_active == 3


async def test_concurrency_is_bounded_by_max_concurrent(db, job):
    cands = mk_cands(db, job, 6)
    for c in cands:
        db.emit_event(job, E.Tick, candidate_id=c)
    h = Recorder(delay=0.05)
    assert await Dispatcher(h, max_concurrent=2).run_once() == 6
    assert h.max_active == 2


async def test_job_level_events_are_serial_per_job(db, job):
    ids = [db.emit_event(job, E.JobCreated) for _ in range(3)]
    h = Recorder(delay=0.02)
    await Dispatcher(h).run_once()
    assert h.seen == ids
    assert h.max_by_cand[f"job:{job}"] == 1


async def test_two_dispatchers_never_double_process_an_event(db, job):
    cands = mk_cands(db, job, 5)
    ids = [db.emit_event(job, E.Tick, candidate_id=c) for c in cands]
    h = Recorder(delay=0.02)
    await asyncio.gather(Dispatcher(h, owner="a").run_once(), Dispatcher(h, owner="b").run_once())
    assert sorted(h.seen) == ids  # each exactly once


# ---------- failures ----------

async def test_handler_exception_counts_an_attempt_and_retries(db, job):
    (cid,) = mk_cands(db, job, 1)
    eid = db.emit_event(job, E.Tick, candidate_id=cid)
    h = Recorder(fail_ids={eid})
    d = Dispatcher(h)
    assert await d.run_once() == 1  # failure must not propagate out of run_once
    assert tuple(status_of(eid)) == ("pending", 1)
    await d.run_once()
    assert tuple(status_of(eid)) == ("pending", 2)


async def test_exhausted_event_is_failed_and_escalated_once(db, job):
    (cid,) = mk_cands(db, job, 1)
    eid = db.emit_event(job, E.Tick, candidate_id=cid)
    escalated = []

    async def on_exhausted(event, exc):
        escalated.append((event.id, str(exc)))

    d = Dispatcher(Recorder(fail_ids={eid}), max_attempts=3, on_exhausted=on_exhausted)
    for _ in range(5):
        await d.run_once()
    assert tuple(status_of(eid)) == ("failed", 3)
    assert escalated == [(eid, f"boom {eid}")]


async def test_failure_does_not_block_other_candidates(db, job):
    a, b = mk_cands(db, job, 2)
    bad = db.emit_event(job, E.Tick, candidate_id=a)
    good = db.emit_event(job, E.Tick, candidate_id=b)
    h = Recorder(fail_ids={bad})
    await Dispatcher(h).run_once()
    assert tuple(status_of(good)) == ("done", 0)


async def test_failed_event_holds_back_that_candidates_later_events(db, job):
    (cid,) = mk_cands(db, job, 1)
    first = db.emit_event(job, E.Tick, candidate_id=cid)
    second = db.emit_event(job, E.Tick, candidate_id=cid)
    h = Recorder(fail_ids={first})
    await Dispatcher(h).run_once()
    assert h.seen == [first], "later event must not jump ahead of a failing earlier one"
    assert status_of(second)["status"] == "pending"


async def test_exception_in_on_exhausted_does_not_crash_the_cycle(db, job):
    a, b = mk_cands(db, job, 2)
    bad = db.emit_event(job, E.Tick, candidate_id=a)
    good = db.emit_event(job, E.Tick, candidate_id=b)

    async def on_exhausted(event, exc):
        raise RuntimeError("escalate failed too")

    d = Dispatcher(Recorder(fail_ids={bad}), max_attempts=1, on_exhausted=on_exhausted)
    await d.run_once()
    assert status_of(bad)["status"] == "failed"
    assert status_of(good)["status"] == "done"


# ---------- per-candidate lock ----------

async def test_candidate_lock_is_held_during_handler_and_released_after(db, job):
    (cid,) = mk_cands(db, job, 1)
    db.emit_event(job, E.Tick, candidate_id=cid)
    inside = []

    async def h(event):
        inside.append(repo.get_candidate(cid)["locked_by"])

    await Dispatcher(h, owner="disp-1").run_once()
    assert inside == ["disp-1"]
    assert repo.get_candidate(cid)["locked_by"] is None


async def test_lock_is_released_when_the_handler_raises(db, job):
    (cid,) = mk_cands(db, job, 1)
    eid = db.emit_event(job, E.Tick, candidate_id=cid)
    await Dispatcher(Recorder(fail_ids={eid})).run_once()
    assert repo.get_candidate(cid)["locked_by"] is None


async def test_event_waits_when_another_owner_holds_the_lock(db, job):
    (cid,) = mk_cands(db, job, 1)
    eid = db.emit_event(job, E.Tick, candidate_id=cid)
    db.acquire_lock(cid, "someone-else")
    h = Recorder()
    d = Dispatcher(h, owner="disp-1")
    await d.run_once()
    assert h.seen == []
    assert tuple(status_of(eid)) == ("pending", 0), "a busy lock is not a failed attempt"
    assert repo.get_candidate(cid)["locked_by"] == "someone-else"
    db.release_lock(cid, "someone-else")
    await d.run_once()
    assert h.seen == [eid]


async def test_job_level_events_take_no_candidate_lock(db, job):
    eid = db.emit_event(job, E.JobCreated)
    h = Recorder()
    await Dispatcher(h).run_once()
    assert h.seen == [eid]


# ---------- background loop: start / stop ----------

async def wait_for(pred, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_start_dispatcher_polls_and_processes_new_events(db, job):
    (cid,) = mk_cands(db, job, 1)
    h = Recorder()
    start_dispatcher(h, poll_interval=0.01)
    first = db.emit_event(job, E.Tick, candidate_id=cid)
    assert await wait_for(lambda: first in h.seen)
    later = db.emit_event(job, E.Tick, candidate_id=cid)  # picked up by a later poll
    assert await wait_for(lambda: later in h.seen)
    stop_dispatcher()


async def test_stop_dispatcher_stops_processing(db, job):
    (cid,) = mk_cands(db, job, 1)
    h = Recorder()
    start_dispatcher(h, poll_interval=0.01)
    stop_dispatcher()
    await asyncio.sleep(0.05)
    eid = db.emit_event(job, E.Tick, candidate_id=cid)
    await asyncio.sleep(0.1)
    assert eid not in h.seen
    assert status_of(eid)["status"] == "pending"


async def test_start_twice_is_a_noop_and_stop_is_idempotent(db, job):
    (cid,) = mk_cands(db, job, 1)
    h = Recorder()
    start_dispatcher(h, poll_interval=0.01)
    start_dispatcher(h, poll_interval=0.01)
    eid = db.emit_event(job, E.Tick, candidate_id=cid)
    assert await wait_for(lambda: eid in h.seen)
    await asyncio.sleep(0.05)
    assert h.seen.count(eid) == 1
    stop_dispatcher()
    stop_dispatcher()


async def test_stop_without_start_does_not_raise(db):
    stop_dispatcher()


async def test_start_dispatcher_can_restart_after_stop(db, job):
    (cid,) = mk_cands(db, job, 1)
    h = Recorder()
    start_dispatcher(h, poll_interval=0.01)
    stop_dispatcher()
    await asyncio.sleep(0.02)
    start_dispatcher(h, poll_interval=0.01)
    eid = db.emit_event(job, E.Tick, candidate_id=cid)
    assert await wait_for(lambda: eid in h.seen)


async def test_loop_survives_a_failing_handler(db, job):
    a, b = mk_cands(db, job, 2)
    bad = db.emit_event(job, E.Tick, candidate_id=a)
    h = Recorder(fail_ids={bad})
    start_dispatcher(h, poll_interval=0.01)
    assert await wait_for(lambda: status_of(bad)["status"] == "failed")
    good = db.emit_event(job, E.Tick, candidate_id=b)  # loop still alive afterwards
    assert await wait_for(lambda: good in h.seen)


# ---------- crash recovery on startup ----------

async def test_start_resets_stuck_processing_events_and_runs_them(db, job):
    (cid,) = mk_cands(db, job, 1)
    eid = db.emit_event(job, E.Tick, candidate_id=cid)
    assert db.claim_event(eid)  # simulate: claimed, then the process died
    with repo.get_conn() as c:
        c.execute("UPDATE events SET processed_at='2000-01-01T00:00:00.000Z' WHERE id=?", (eid,))
    h = Recorder()
    start_dispatcher(h, poll_interval=0.01)
    assert await wait_for(lambda: eid in h.seen)
    assert await wait_for(lambda: status_of(eid)["status"] == "done")


async def test_start_does_not_touch_fresh_processing_events(db, job):
    (cid,) = mk_cands(db, job, 1)
    eid = db.emit_event(job, E.Tick, candidate_id=cid)
    db.claim_event(eid)  # claimed just now by a live worker
    h = Recorder()
    start_dispatcher(h, poll_interval=0.01)
    await asyncio.sleep(0.1)
    assert h.seen == [] and status_of(eid)["status"] == "processing"


async def test_start_releases_stale_candidate_locks(db, job):
    (cid,) = mk_cands(db, job, 1)
    db.acquire_lock(cid, "dead-process")
    with repo.get_conn() as c:
        c.execute("UPDATE candidates SET locked_at='2000-01-01T00:00:00.000Z' WHERE id=?", (cid,))
    eid = db.emit_event(job, E.Tick, candidate_id=cid)
    h = Recorder()
    start_dispatcher(h, poll_interval=0.01)
    assert await wait_for(lambda: eid in h.seen)
