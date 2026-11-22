"""Add a few mock candidates to an existing role, for checking the dashboard sidebar (lane C).

    python scripts/seed_candidates.py                 # newest role
    python scripts/seed_candidates.py --job <job_id>  # a specific role

Unlike reset_demo.py this does not wipe anything. It writes candidates, their memory and a
CandidateFound event each, spread across pipeline stages. Run it again to add another batch.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.db.queries import db_path  # noqa: E402
from scripts.reset_demo import Seeder, _name_key, ulid  # noqa: E402

# name, headline, state, score, skills
MOCK_PEOPLE = [
    ("Maya Lindqvist", "ML engineer @ Clio", "DISCOVERED", None, ["Python", "RAG"]),
    ("Rohan Mehta", "Search infra engineer @ Faire", "RESEARCHED", None, ["TypeScript", "Elasticsearch", "RAG"]),
    ("Elena Petrova", "AI engineer @ Cohere", "INTERESTED", None, ["RAG", "LLM evaluation", "Python"]),
    ("Sam Adeyemi", "Full-stack engineer @ Wealthsimple", "SHORTLISTED", 84, ["TypeScript", "React", "RAG"]),
    ("Noah Tremblay", "Backend engineer @ Lightspeed", "EVALUATED", 64, ["Go", "Postgres", "TypeScript"]),
]

# Evaluations for the scored mock candidates, so the shortlist shows a breakdown.
EVALUATIONS = {
    "Sam Adeyemi": ("shortlist", "Shipped a RAG feature end to end in TypeScript; measures retrieval quality.", [
        {"name": "RAG systems", "weight": 0.3, "score": 86, "evidence": "Built doc search with re-ranking for support tickets."},
        {"name": "TypeScript / React", "weight": 0.25, "score": 90, "evidence": "Owns the Next.js admin app."},
        {"name": "LLM evaluation", "weight": 0.2, "score": 78, "evidence": "Weekly eval set of 150 labelled queries."},
        {"name": "Ownership", "weight": 0.15, "score": 82, "evidence": "Led the feature from spec to launch."},
        {"name": "Startup fit", "weight": 0.1, "score": 80, "evidence": "Asked sharp questions about runway and scope."},
    ]),
    "Noah Tremblay": ("reject", "Strong backend engineer, but no hands-on retrieval or eval work yet.", [
        {"name": "RAG systems", "weight": 0.3, "score": 45, "evidence": "Read about it; hasn't built one."},
        {"name": "TypeScript / React", "weight": 0.25, "score": 70, "evidence": "Some TypeScript services, little UI."},
        {"name": "LLM evaluation", "weight": 0.2, "score": 50, "evidence": "No examples given."},
        {"name": "Ownership", "weight": 0.15, "score": 88, "evidence": "Ran the payments migration."},
        {"name": "Startup fit", "weight": 0.1, "score": 80, "evidence": "Keen on an early-stage role."},
    ]),
}


def _ensure_evaluation(s: Seeder, job_id: str, cid: str, name: str, score: int | None) -> None:
    if score is None or name not in EVALUATIONS:
        return
    if s.conn.execute("SELECT 1 FROM evaluations WHERE candidate_id = ?", (cid,)).fetchone():
        return
    rec, summary, criteria = EVALUATIONS[name]
    s.insert("evaluations", id=ulid(), candidate_id=cid, job_id=job_id, score=score, criteria=criteria,
             recommendation=rec, summary=summary, created_at=s.tick(2))


def seed_candidates(conn: sqlite3.Connection, job_id: str | None = None) -> tuple[str, list[str]]:
    if job_id is None:
        row = conn.execute("SELECT id FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        if not row:
            raise SystemExit("[seed_candidates] no roles yet; create one in the UI or run reset_demo.py")
        job_id = row[0]
    elif not conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
        raise SystemExit(f"[seed_candidates] no role with id {job_id}")

    taken = {r[0]: r[1] for r in conn.execute("SELECT name_key, id FROM candidates WHERE job_id = ?", (job_id,))}
    s = Seeder(conn, datetime.now(UTC) - timedelta(minutes=5))
    added: list[str] = []
    for name, headline, state, score, skills in MOCK_PEOPLE:
        key = _name_key(name)
        if key in taken:  # keep reruns idempotent per role; only backfill a missing evaluation
            _ensure_evaluation(s, job_id, taken[key], name, score)
            continue
        cid = ulid()
        ts = s.tick(3)
        first = name.split()[0].lower()
        s.insert(
            "candidates",
            id=cid, job_id=job_id, ploid_id=f"mock_{key}", name=name, name_key=key,
            state=state, score=score, created_at=ts, updated_at=ts,
        )
        s.insert(
            "candidate_memory",
            candidate_id=cid,
            profile={
                "headline": headline,
                "skills": skills,
                "projects": [],
                "education": [],
                "links": {"github": f"https://github.com/{first}-dev"},
                "completeness": 0.85,
            },
            research_notes={},
            contact={},
            contact_log=[],
            decisions=[],
            summary=f"{name}: {headline}.",
            updated_at=ts,
        )
        s.event(job_id, cid, "CandidateFound",
                {"ploid_id": f"mock_{key}", "name": name,
                 "decision": {"action": "search_candidates", "reasoning": "Mock candidate added by seed_candidates.py."}},
                gap=3)
        _ensure_evaluation(s, job_id, cid, name, score)
        added.append(name)
    conn.commit()
    return job_id, added


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", help="role id (default: newest role)")
    args = ap.parse_args()
    conn = sqlite3.connect(db_path())
    job_id, added = seed_candidates(conn, args.job)
    print(f"[seed_candidates] {db_path()}: added {len(added)} to job {job_id}: {', '.join(added) or 'none (already there)'}")
    print(f"[seed_candidates] open {os.environ.get('WEB_URL', 'http://localhost:3000')}/jobs/{job_id}")


if __name__ == "__main__":
    main()
