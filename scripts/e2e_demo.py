"""End-to-end demo flow through the real app (dispatcher + agent + tools), with auto-approve on.

Offline by default (LLM_MODE=stub, mock tools, short delays). Use against real models with:
    LLM_MODE=real .venv/bin/python scripts/e2e_demo.py
Prints each candidate's state every few seconds until the job finishes or the timeout hits.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

tmp = tempfile.mkdtemp(prefix="airecruiter-e2e-")
defaults = {
    "DATA_DIR": tmp, "CHECKPOINT_DB": f"{tmp}/checkpoints.db", "LLM_MODE": "stub", "TOOLS_MODE": "mock",
    "ELEVENLABS_MODE": "mock", "ELEVENLABS_POLL": "true", "MOCK_INTERVIEW_DELAY_S": "2",
    "PERSONA_DELAY_S": "1", "TICK_INTERVAL_S": "1", "DEMO_TOKEN": "",
}
for k, v in defaults.items():
    os.environ.setdefault(k, v)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402

TIMEOUT_S = float(os.environ.get("E2E_TIMEOUT_S", "120"))


def main() -> int:
    with TestClient(app) as c:
        r = c.post("/api/jobs", json={"title": "AI Engineer", "mustHaves": ["RAG", "TypeScript"],
                                      "niceToHaves": ["LangGraph"]})
        assert r.status_code == 201, r.text
        job = r.json()
        jid = job["id"]
        print("job", jid, "rubric:", [cr["name"] for cr in (job.get("rubric") or {}).get("criteria", [])])
        policy = job.get("permissionPolicy") or {}
        r = c.patch(f"/api/jobs/{jid}", json={"permissionPolicy": {**policy, "autoApprove": True,
                                                                   "autoApproveActions": []}})
        assert r.status_code == 200, r.text
        r = c.post(f"/api/jobs/{jid}/start")
        assert r.status_code == 200, r.text

        t0, last = time.time(), None
        while time.time() - t0 < TIMEOUT_S:
            time.sleep(3)
            cands = c.get(f"/api/jobs/{jid}/candidates").json()
            states = {x["name"]: x["state"] for x in cands}
            if states != last:
                print(f"t={time.time() - t0:5.1f}s", dict(Counter(states.values())))
                last = states
            pending = c.get(f"/api/jobs/{jid}/approvals", params={"status": "pending"}).json()
            for a in pending:  # restricted actions that auto-approve won't take (e.g. reject proposals)
                c.post(f"/api/approvals/{a['id']}/approve", json={})
            if cands and all(s in {"SHORTLISTED", "REJECTED", "DECLINED", "STALE", "NEEDS_HUMAN"}
                             for s in states.values()):
                break
        print("\nFINAL:")
        for x in c.get(f"/api/jobs/{jid}/candidates").json():
            print(f"  {x['name']:16} {x['state']:14} score={x.get('score')}")
        stuck = [n for n, s in (last or {}).items() if s == "NEEDS_HUMAN"]
        for x in c.get(f"/api/jobs/{jid}/candidates").json():
            if x["state"] == "NEEDS_HUMAN":
                d = c.get(f"/api/candidates/{x['id']}").json()
                print("   NEEDS_HUMAN", x["name"], "->", str(d.get("events", [])[-1:])[:300])
        shortlist = c.get(f"/api/jobs/{jid}/shortlist").json()
        print("shortlist:", [(s.get("name"), s.get("score")) for s in shortlist])
        return 0 if shortlist and not stuck else 1


if __name__ == "__main__":
    raise SystemExit(main())
