"""End-to-end smoke run (lane C): create a role, turn auto-approve on, start, wait for a shortlist.

It makes the same calls the dashboard's New Role page makes (POST /jobs -> PATCH -> POST /start),
then only watches. Nothing else is called, so this also checks the acceptance criterion:
a role created in the dashboard produces >= 3 candidates that progress with no manual API calls.

    python scripts/smoke.py                       # in-process app, temp DB, mock tools, stub LLM
    python scripts/smoke.py --until OUTREACH_DRAFTED
    python scripts/smoke.py --url http://localhost:8000      # against a running server
    python scripts/smoke.py --real-llm            # in-process, but Claude makes the decisions

Exit code 0 when a candidate reaches --until (default SHORTLISTED) and >= 3 candidates moved
past DISCOVERED; 1 otherwise.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Furthest-progress order. Off-path states rank by where they branch off.
PROGRESS = [
    "DISCOVERED", "RESEARCHED", "CONTACT_FOUND", "OUTREACH_DRAFTED", "APPROVED", "CONTACTED",
    "INTERESTED", "INTERVIEWING", "INTERVIEWED", "EVALUATED", "SHORTLISTED",
]
RANK = {s: i for i, s in enumerate(PROGRESS)} | {"DECLINED": 5, "STALE": 5, "REJECTED": 9, "NEEDS_HUMAN": -1}

ROLE = {
    "title": "Founding AI Engineer",
    "mustHaves": ["Python", "LLM applications", "shipped a product end to end"],
    "niceToHaves": ["TypeScript", "startup experience"],
    "location": "Remote",
    "seniority": "mid-senior",
    "freeText": "Smoke test role.",
}


def log(msg: str) -> None:
    print(f"[smoke {time.strftime('%H:%M:%S')}] {msg}", flush=True)


@contextmanager
def in_process_client(real_llm: bool) -> Iterator[Any]:
    """Boot the real FastAPI app (dispatcher included) against a throwaway DB."""
    tmp = tempfile.TemporaryDirectory(prefix="airecruiter-smoke-", ignore_cleanup_errors=True)
    # Must be set before api.* is imported: settings are read once at import time.
    os.environ.update({
        "DATA_DIR": tmp.name,
        "RECRUITER_DB": str(Path(tmp.name) / "recruiter.db"),
        "TOOLS_MODE": "mock",
        "LLM_MODE": "real" if real_llm else "stub",
        "DISPATCHER_ENABLED": "true",
        "PERSONA_DELAY_S": os.environ.get("PERSONA_DELAY_S", "1"),
        "MOCK_INTERVIEW_DELAY_S": os.environ.get("MOCK_INTERVIEW_DELAY_S", "2"),
    })
    os.environ.pop("DEMO_TOKEN", None)
    os.environ.pop("FAIL_TOOL", None)

    from fastapi.testclient import TestClient

    from api.db import repo
    from api.main import create_app

    repo.init_db(Path(tmp.name) / "recruiter.db")
    log(f"in-process app, DB in {tmp.name} (LLM_MODE={os.environ['LLM_MODE']}, TOOLS_MODE=mock)")
    try:
        with TestClient(create_app()) as c:
            yield c
    finally:
        tmp.cleanup()


@contextmanager
def remote_client(url: str) -> Iterator[Any]:
    import httpx

    headers = {"X-Demo-Token": t} if (t := os.environ.get("DEMO_TOKEN")) else {}
    log(f"remote app at {url}")
    with httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=30) as c:
        yield c


def call(c: Any, method: str, path: str, **kw: Any) -> Any:
    r = c.request(method, f"/api{path}", **kw)
    if r.status_code >= 400:
        raise SystemExit(f"[smoke] {method} {path} -> {r.status_code}: {r.text[:500]}")
    return r.json()


def run(c: Any, until: str, timeout: float, min_progressed: int) -> bool:
    health = c.get("/health").json()
    log(f"health: {health}")

    # Same three calls as web/components/NewRole.tsx.
    job = call(c, "POST", "/jobs", json=ROLE)
    jid = job["id"]
    log(f"created job {jid}: {len(job['rubric']['criteria'])} rubric criteria")
    policy = {**job["permissionPolicy"], "autoApprove": True}
    call(c, "PATCH", f"/jobs/{jid}", json={
        "rubric": job["rubric"], "shortlistTarget": 1, "scoreThreshold": job["scoreThreshold"],
        "permissionPolicy": policy,
    })
    call(c, "POST", f"/jobs/{jid}/start")
    log("started with auto-approve on; watching (no further writes)")

    seen: dict[str, str] = {}
    deadline = time.monotonic() + timeout
    ok = False
    while time.monotonic() < deadline:
        cands = call(c, "GET", f"/jobs/{jid}/candidates")
        for cand in cands:
            if seen.get(cand["id"]) != cand["state"]:
                prev = seen.get(cand["id"], "new")
                seen[cand["id"]] = cand["state"]
                extra = f" score={cand['score']}" if cand.get("score") is not None else ""
                log(f"  {cand['name']:<22} {prev} -> {cand['state']}{extra}")
        progressed = sum(1 for s in seen.values() if RANK.get(s, 0) > 0)
        if any(s == until for s in seen.values()) and progressed >= min_progressed:
            ok = True
            break
        time.sleep(1)

    approvals = call(c, "GET", f"/jobs/{jid}/approvals")
    pending = [a for a in approvals if a["status"] == "pending"]
    events = call(c, "GET", f"/jobs/{jid}/events")
    progressed = sum(1 for s in seen.values() if RANK.get(s, 0) > 0)
    furthest = max(seen.values(), key=lambda s: RANK.get(s, -1), default="none")

    print()
    log(f"candidates: {len(seen)} | moved past DISCOVERED: {progressed} | furthest: {furthest}")
    log(f"events: {len(events)} | approvals: {len(approvals)} ({len(pending)} still pending)")
    by_state: dict[str, int] = {}
    for s in seen.values():
        by_state[s] = by_state.get(s, 0) + 1
    log("by state: " + ", ".join(f"{k}={v}" for k, v in sorted(by_state.items(), key=lambda kv: RANK.get(kv[0], -1))))

    if ok:
        log(f"PASS: reached {until} with {progressed} candidates progressing on their own")
        return True
    reasons = []
    if not any(s == until for s in seen.values()):
        reasons.append(f"no candidate reached {until} within {timeout:.0f}s")
    if progressed < min_progressed:
        reasons.append(f"only {progressed} candidates moved past DISCOVERED (need {min_progressed})")
    if pending:
        reasons.append(f"{len(pending)} approval(s) left pending although auto-approve is on "
                       f"({', '.join(sorted({a['action'] for a in pending}))})")
    stuck = [s for s in seen.values() if s == "NEEDS_HUMAN"]
    if stuck:
        reasons.append(f"{len(stuck)} candidate(s) escalated to NEEDS_HUMAN")
    log("FAIL: " + "; ".join(reasons))
    return False


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="run against this server instead of an in-process app")
    p.add_argument("--until", default="SHORTLISTED", choices=PROGRESS, help="state that counts as success")
    p.add_argument("--timeout", type=float, default=120, help="seconds to wait (default 120)")
    p.add_argument("--min-progressed", type=int, default=3,
                   help="candidates that must move past DISCOVERED (acceptance criterion, default 3)")
    p.add_argument("--real-llm", action="store_true", help="in-process only: LLM_MODE=real instead of stub")
    a = p.parse_args()

    ctx = remote_client(a.url) if a.url else in_process_client(a.real_llm)
    with ctx as c:
        ok = run(c, a.until, a.timeout, a.min_progressed)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
