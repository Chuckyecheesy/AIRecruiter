"""Red tests for the repo.py / dispatcher signature stubs (lane A kickoff).

Lane C imports these before the bodies exist, so the contract here is the
public signature only: names, parameter order, required vs optional, sync.
Source: specs/team/person-a-agent-core.md (kickoff), 03-permissions.md.
"""
import inspect

import pytest

from api.db import repo

# name: (required positional params in order, optional params with their defaults)
REPO_API = {
    "create_job": (["title", "form"], {"rubric": None, "permission_policy": None,
                                       "shortlist_target": 3, "score_threshold": 75,
                                       "max_candidates": 5}),
    "update_job": (["job_id"], {}),  # plus **fields
    "set_job_state": (["job_id", "state"], {}),
    "emit_event": (["job_id", "type"], {"candidate_id": None, "payload": None, "not_before": None}),
    "decide_approval": (["approval_id", "approved"], {"payload": None, "feedback": None}),
    "resume_candidate": (["candidate_id"], {}),
    "skip_candidate": (["candidate_id"], {}),
    "set_demo_disposition": (["candidate_id", "disposition"], {}),
}


@pytest.mark.parametrize("name", sorted(REPO_API))
def test_function_exists_and_is_sync(name):
    fn = getattr(repo, name)
    assert callable(fn)
    assert not inspect.iscoroutinefunction(fn)  # sqlite3 writes; routes call directly


@pytest.mark.parametrize("name", sorted(REPO_API))
def test_signature_matches(name):
    required, optional = REPO_API[name]
    params = [p for p in inspect.signature(getattr(repo, name)).parameters.values()
              if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)]
    assert [p.name for p in params if p.default is p.empty] == required
    assert {p.name: p.default for p in params if p.default is not p.empty} == optional


def test_update_job_accepts_arbitrary_field_updates():
    kinds = [p.kind for p in inspect.signature(repo.update_job).parameters.values()]
    assert inspect.Parameter.VAR_KEYWORD in kinds


def test_emit_event_type_is_not_shadowing_via_positional_order():
    # job_id first, type second: C calls emit_event(job_id, EventType.X, candidate_id=...)
    names = list(inspect.signature(repo.emit_event).parameters)
    assert names[:2] == ["job_id", "type"]


def test_dispatcher_exports_start_and_stop():
    from api.agent import dispatcher
    for name in ("start_dispatcher", "stop_dispatcher"):
        assert callable(getattr(dispatcher, name))
