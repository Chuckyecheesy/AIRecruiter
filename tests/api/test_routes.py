"""Smoke tests for every lane C route against the seeded showcase job."""

from __future__ import annotations

import json


def _approval(client, job_id, action):
    pending = client.get(f"/api/jobs/{job_id}/approvals", params={"status": "pending"}).json()
    return next(a for a in pending if a["action"] == action)


def test_health(client):
    for path in ("/health", "/api/health"):
        r = client.get(path)
        assert r.status_code == 200
        assert r.json()["db"] == "ok"


def test_jobs_list_and_detail(client, seeded):
    jobs = client.get("/api/jobs").json()
    assert [j["id"] for j in jobs] == [seeded]
    assert jobs[0]["counts"]["SHORTLISTED"] == 1
    assert jobs[0]["pendingApprovals"] == 4

    job = client.get(f"/api/jobs/{seeded}").json()
    assert job["state"] == "ACTIVE"
    assert job["permissionPolicy"]["autoApprove"] is False
    assert len(job["rubric"]["criteria"]) == 5
    assert client.get("/api/jobs/nope").status_code == 404


def test_create_patch_start_pause_resume(client):
    r = client.post("/api/jobs", json={"title": "Founding Designer", "mustHaves": ["Figma"], "niceToHaves": ["React"]})
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["state"] == "DRAFT"
    assert job["rubric"]["criteria"], "stub rubric generated"
    jid = job["id"]

    r = client.patch(f"/api/jobs/{jid}", json={"scoreThreshold": 80, "permissionPolicy": {"autoApprove": True}})
    assert r.status_code == 200, r.text
    assert r.json()["scoreThreshold"] == 80
    assert r.json()["permissionPolicy"]["autoApprove"] is True

    assert client.post(f"/api/jobs/{jid}/start").json()["state"] == "ACTIVE"
    assert client.post(f"/api/jobs/{jid}/start").status_code == 409
    events = client.get(f"/api/jobs/{jid}/events").json()
    assert [e["type"] for e in events] == ["JobCreated"]

    assert client.post(f"/api/jobs/{jid}/pause").json()["state"] == "PAUSED"
    assert client.post(f"/api/jobs/{jid}/resume").json()["state"] == "ACTIVE"


def test_candidates(client, seeded):
    cands = client.get(f"/api/jobs/{seeded}/candidates").json()
    assert len(cands) == 9
    by_name = {c["name"]: c for c in cands}
    assert by_name["Tom Okafor"]["needsHuman"] is True
    assert by_name["Tom Okafor"]["traceUrl"]
    assert by_name["Marcus Webb"]["pendingApprovalId"]
    assert by_name["Sarah Chen"]["headline"]

    sarah = client.get(f"/api/candidates/{by_name['Sarah Chen']['id']}").json()
    assert sarah["evaluation"]["score"] == 92
    assert sarah["interview"]["status"] == "completed"
    assert len(sarah["messages"]) == 2
    assert sarah["memory"]["decisions"]
    assert any(e["type"] == "Evaluated" for e in sarah["events"])


def test_candidate_resume_skip_patch(client, seeded):
    by_name = {c["name"]: c for c in client.get(f"/api/jobs/{seeded}/candidates").json()}
    tom = by_name["Tom Okafor"]["id"]
    r = client.post(f"/api/candidates/{tom}/resume")
    assert r.status_code == 200 and r.json()["state"] == "RESEARCHED"
    assert client.post(f"/api/candidates/{tom}/resume").status_code == 409

    kim = by_name["Kim Lee"]["id"]
    assert client.post(f"/api/candidates/{kim}/skip").json()["state"] == "REJECTED"
    assert client.post(f"/api/candidates/{kim}/skip").status_code == 409

    marcus = by_name["Marcus Webb"]["id"]
    r = client.patch(f"/api/candidates/{marcus}", json={"demoDisposition": "not_interested"})
    assert r.json()["demoDisposition"] == "not_interested"


def test_approve_with_edit_and_reject(client, seeded):
    a = _approval(client, seeded, "send_outreach")
    edited = {**a["payload"], "body": "Edited by CEO"}
    r = client.post(f"/api/approvals/{a['id']}/approve", json={"payload": edited})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["payload"]["body"] == "Edited by CEO"
    assert client.post(f"/api/approvals/{a['id']}/approve").status_code == 409

    b = _approval(client, seeded, "schedule_interview")
    r = client.post(f"/api/approvals/{b['id']}/reject", json={"feedback": "not yet"})
    assert r.json()["status"] == "rejected" and r.json()["feedback"] == "not yet"

    events = client.get(f"/api/jobs/{seeded}/events").json()
    granted = [e for e in events if e["type"] == "ApprovalGranted" and e["approvalId"] == a["id"]]
    rejected = [e for e in events if e["type"] == "ApprovalRejected" and e["approvalId"] == b["id"]]
    assert granted and rejected


def test_events_poll_cursor_and_delayed(client, seeded):
    events = client.get(f"/api/jobs/{seeded}/events").json()
    assert len(events) > 30
    assert all(e["type"] != "Tick" for e in events)
    ids = [e["id"] for e in events]
    assert ids == sorted(ids)
    assert any(e["payload"].get("duplicates_skipped") for e in events)
    req = next(e for e in events if e["type"] == "ApprovalRequested")
    assert req["approvalId"]

    last = ids[-1]
    assert client.get(f"/api/jobs/{seeded}/events", params={"after": last}).json() == []

    # A delayed event is withheld until due.
    from scripts import dev_fake_repo

    dev_fake_repo.emit_event(seeded, None, "EmailReplied", {"body": "later"}, not_before="2999-01-01T00:00:00+00:00")
    dev_fake_repo.emit_event(seeded, None, "EmailSent", {"now": True})
    fresh = client.get(f"/api/jobs/{seeded}/events", params={"after": last}).json()
    assert [e["type"] for e in fresh] == ["EmailSent"]


def test_sse_stream_sends_events(client, seeded, monkeypatch):
    from api.routes import events as events_mod

    class Once:
        n = 0

    async def disconnected_after_first(self):
        Once.n += 1
        return Once.n > 1

    monkeypatch.setattr("starlette.requests.Request.is_disconnected", disconnected_after_first)
    monkeypatch.setattr(events_mod, "POLL_INTERVAL_S", 0)
    with client.stream("GET", f"/api/jobs/{seeded}/stream") as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    data_lines = [ln[len("data: "):] for ln in body.splitlines() if ln.startswith("data: ")]
    assert data_lines, body[:500]
    first = json.loads(data_lines[0])
    assert first["type"] == "JobCreated"
    assert "id: " in body


def test_chat_feedback_rejects_focused_approval(client, seeded):
    a = _approval(client, seeded, "send_outreach")
    r = client.post(f"/api/jobs/{seeded}/chat",
                    json={"text": "shorter, mention the AI study assistant", "approvalId": a["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["actionTaken"] == "reject"
    got = client.get(f"/api/approvals/{a['id']}").json()
    assert got["status"] == "rejected"
    assert got["feedback"] == "shorter, mention the AI study assistant"


def test_chat_commands(client, seeded):
    r = client.post(f"/api/jobs/{seeded}/chat", json={"text": "status"}).json()
    assert r["actionTaken"] == "status" and "Sarah Chen" in r["reply"]
    assert client.post(f"/api/jobs/{seeded}/chat", json={"text": "pause"}).json()["actionTaken"] == "pause"
    assert client.get(f"/api/jobs/{seeded}").json()["state"] == "PAUSED"
    assert client.post(f"/api/jobs/{seeded}/chat", json={"text": "resume"}).json()["actionTaken"] == "resume"

    a = _approval(client, seeded, "send_followup")
    r = client.post(f"/api/jobs/{seeded}/chat", json={"text": "approve", "approvalId": a["id"]}).json()
    assert r["actionTaken"] == "approve"


def test_shortlist(client, seeded):
    sl = client.get(f"/api/jobs/{seeded}/shortlist").json()
    assert sl[0]["candidate"]["name"] == "Sarah Chen"
    assert sl[0]["evaluation"]["criteria"]


def test_admin_reset(client, monkeypatch):
    r = client.post("/api/admin/reset")
    assert r.status_code == 200 and r.json()["jobId"]
    monkeypatch.setenv("ADMIN_ENABLED", "false")
    assert client.post("/api/admin/reset").status_code == 403


def test_repo_missing_gives_503(seeded):
    import importlib.util

    import pytest

    if importlib.util.find_spec("api.db.repo"):
        pytest.skip("lane A repo is merged")
    from fastapi.testclient import TestClient

    from api.main import create_app
    from api.routes import deps

    deps.set_repo(None)
    with TestClient(create_app()) as c:
        r = c.post(f"/api/jobs/{seeded}/pause")
        assert r.status_code == 503
