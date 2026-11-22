"""Red tests for the per-candidate lock helpers (lane A, core loop).

Basic acquire/release/stale/race behavior is already covered in test_repo.py.
These cover what the dispatcher needs on top. Assumed API (implement to match):
    LockBusy(RuntimeError)                      raised when the lock is held by someone else
    candidate_lock(cid, owner, stale_after_s=120)
                                                context manager: acquire or raise LockBusy;
                                                always releases on exit, even on error
    refresh_lock(cid, owner) -> bool            heartbeat: bump locked_at if still the owner
    release_stale_locks(stale_after_s=120) -> int
                                                startup crash recovery: clear locks older than
                                                the threshold, return how many were cleared
    release_lock must not raise for unknown candidates or non-owners.
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest

from api.db import repo


@pytest.fixture
def db(tmp_path):
    repo.init_db(tmp_path / "recruiter.db")
    return repo


@pytest.fixture
def job(db):
    return db.create_job("Role", {})


@pytest.fixture
def cand(db, job):
    return db.create_candidate(job, "p-1", "Sarah Chen")


def age_lock(cid, seconds):
    ts = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with repo.get_conn() as c:
        c.execute("UPDATE candidates SET locked_at=? WHERE id=?", (ts, cid))


def lock_row(cid):
    c = repo.get_candidate(cid)
    return c["locked_by"], c["locked_at"]


# ---------- candidate_lock context manager ----------

def test_context_manager_holds_then_releases(db, cand):
    with db.candidate_lock(cand, "w1"):
        assert lock_row(cand)[0] == "w1"
    assert lock_row(cand) == (None, None)


def test_context_manager_releases_on_exception(db, cand):
    with pytest.raises(RuntimeError):
        with db.candidate_lock(cand, "w1"):
            raise RuntimeError("tool blew up")
    assert lock_row(cand) == (None, None)
    assert db.acquire_lock(cand, "w2") is True


def test_context_manager_raises_lockbusy_when_held(db, cand):
    db.acquire_lock(cand, "w1")
    with pytest.raises(db.LockBusy):
        with db.candidate_lock(cand, "w2"):
            pytest.fail("body must not run while another owner holds the lock")
    assert lock_row(cand)[0] == "w1"  # the failed attempt must not steal or clear it


def test_lockbusy_is_a_runtime_error():
    assert issubclass(repo.LockBusy, RuntimeError)


def test_context_manager_takes_over_stale_lock(db, cand):
    db.acquire_lock(cand, "dead")
    age_lock(cand, 300)
    with db.candidate_lock(cand, "w2"):
        assert lock_row(cand)[0] == "w2"


def test_context_manager_unknown_candidate(db):
    with pytest.raises(LookupError):
        with db.candidate_lock("missing", "w"):
            pass


def test_nested_same_owner_does_not_release_early(db, cand):
    with db.candidate_lock(cand, "w1"):
        with db.candidate_lock(cand, "w1"):
            pass
        assert lock_row(cand)[0] == "w1", "inner exit released the outer lock"
    assert lock_row(cand) == (None, None)


def test_only_one_thread_enters_the_critical_section(db, cand):
    inside, max_inside, busy = [0], [0], [0]
    guard = threading.Lock()

    def work(i):
        try:
            with db.candidate_lock(cand, f"w{i}"):
                with guard:
                    inside[0] += 1
                    max_inside[0] = max(max_inside[0], inside[0])
                threading.Event().wait(0.05)
                with guard:
                    inside[0] -= 1
        except db.LockBusy:
            with guard:
                busy[0] += 1

    ts = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert max_inside[0] == 1
    assert busy[0] >= 1


def test_locks_on_different_candidates_are_independent(db, job):
    a = db.create_candidate(job, "p-a", "A")
    b = db.create_candidate(job, "p-b", "B")
    with db.candidate_lock(a, "w1"):
        with db.candidate_lock(b, "w2"):
            assert lock_row(a)[0] == "w1" and lock_row(b)[0] == "w2"


# ---------- refresh_lock (heartbeat for long tool calls) ----------

def test_refresh_bumps_locked_at_for_owner(db, cand):
    db.acquire_lock(cand, "w1")
    age_lock(cand, 100)
    old = lock_row(cand)[1]
    assert db.refresh_lock(cand, "w1") is True
    assert lock_row(cand)[1] > old


def test_refresh_keeps_a_slow_run_from_going_stale(db, cand):
    db.acquire_lock(cand, "w1")
    age_lock(cand, 110)
    db.refresh_lock(cand, "w1")
    assert db.acquire_lock(cand, "w2", stale_after_s=120) is False


def test_refresh_by_non_owner_is_refused(db, cand):
    db.acquire_lock(cand, "w1")
    assert db.refresh_lock(cand, "w2") is False
    assert lock_row(cand)[0] == "w1"


def test_refresh_without_a_lock_is_refused(db, cand):
    assert db.refresh_lock(cand, "w1") is False
    assert lock_row(cand) == (None, None)


def test_refresh_after_takeover_tells_the_old_owner(db, cand):
    db.acquire_lock(cand, "slow")
    age_lock(cand, 300)
    db.acquire_lock(cand, "fast")
    assert db.refresh_lock(cand, "slow") is False


# ---------- release_lock edge cases ----------

def test_release_unknown_candidate_does_not_raise(db):
    db.release_lock("missing", "w1")


def test_release_when_unlocked_is_a_noop(db, cand):
    db.release_lock(cand, "w1")
    assert lock_row(cand) == (None, None)


# ---------- release_stale_locks (startup crash recovery) ----------

def test_release_stale_locks_clears_only_old_ones(db, job):
    old = db.create_candidate(job, "p-old", "Old")
    fresh = db.create_candidate(job, "p-fresh", "Fresh")
    free = db.create_candidate(job, "p-free", "Free")
    db.acquire_lock(old, "dead")
    db.acquire_lock(fresh, "alive")
    age_lock(old, 600)
    assert db.release_stale_locks(stale_after_s=120) == 1
    assert lock_row(old) == (None, None)
    assert lock_row(fresh)[0] == "alive"
    assert lock_row(free) == (None, None)


def test_release_stale_locks_returns_zero_when_nothing_to_do(db, cand):
    assert db.release_stale_locks() == 0
    db.acquire_lock(cand, "w1")
    assert db.release_stale_locks() == 0


def test_release_stale_locks_threshold_is_respected(db, cand):
    db.acquire_lock(cand, "w1")
    age_lock(cand, 30)
    assert db.release_stale_locks(stale_after_s=60) == 0
    assert db.release_stale_locks(stale_after_s=10) == 1


def test_release_stale_locks_treats_null_locked_at_as_stale(db, cand):
    with repo.get_conn() as c:
        c.execute("UPDATE candidates SET locked_by='ghost', locked_at=NULL WHERE id=?", (cand,))
    assert db.release_stale_locks() == 1
    assert lock_row(cand) == (None, None)


def test_release_stale_locks_does_not_touch_other_columns(db, job):
    cid = db.create_candidate(job, "p-x", "X", state=repo.CandidateState.CONTACTED)
    db.acquire_lock(cid, "dead")
    age_lock(cid, 600)
    db.release_stale_locks()
    c = db.get_candidate(cid)
    assert c["state"] == "CONTACTED" and c["name"] == "X"
