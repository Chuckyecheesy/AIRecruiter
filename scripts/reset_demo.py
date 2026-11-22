"""Reset the demo DB and seed it (lane C).

    python scripts/reset_demo.py              # wipe + seed the showcase job (UI dev data)
    python scripts/reset_demo.py --empty      # wipe only

The showcase job has candidates spread across every interesting state, one pending
approval of each restricted type, and a realistic event history. It lets the dashboard
be built before the agent loop works.

The DB is recreated from api/db/schema.sql (lane A). Until that is merged it falls back
to tests/api/dev_schema.sql.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.db.queries import db_path  # noqa: E402

SCHEMA = ROOT / "api" / "db" / "schema.sql"
DEV_SCHEMA = ROOT / "tests" / "api" / "dev_schema.sql"

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid(ts: datetime | None = None) -> str:
    ms = int((ts or datetime.now(UTC)).timestamp() * 1000)
    n = (ms << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def schema_sql() -> str:
    path = SCHEMA if SCHEMA.exists() else DEV_SCHEMA
    if path == DEV_SCHEMA:
        print(f"[reset_demo] {SCHEMA.relative_to(ROOT)} not found, using dev fallback schema")
    return path.read_text(encoding="utf-8")


def _drop_all(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        conn.execute(f'DROP TABLE IF EXISTS "{t}"')
    conn.commit()


def reset_db(path: Path | None = None) -> sqlite3.Connection:
    """Drop every table and recreate from the schema. Works while other connections are open."""
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    _drop_all(conn)
    conn.executescript(schema_sql())
    conn.commit()

    ckpt = path.parent / "checkpoints.db"
    for f in (ckpt, ckpt.with_suffix(".db-wal"), ckpt.with_suffix(".db-shm")):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            print(f"[reset_demo] could not delete {f.name} (in use); stale checkpoints may remain")
    return conn


# ───────────────────────────── seed helpers ─────────────────────────────


class Seeder:
    def __init__(self, conn: sqlite3.Connection, start: datetime):
        self.conn = conn
        self.t = start

    def tick(self, seconds: float = 20) -> str:
        self.t += timedelta(seconds=seconds)
        return iso(self.t)

    def insert(self, table: str, **row) -> None:
        for k, v in row.items():
            if isinstance(v, (dict, list)):
                row[k] = json.dumps(v)
            elif isinstance(v, bool):
                row[k] = int(v)
        cols = ", ".join(row)
        qs = ", ".join("?" for _ in row)
        self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(row.values()))

    def event(self, job_id: str, cand: str | None, type_: str, payload: dict | None = None, *, gap: float = 15,
              not_before: str | None = None) -> int:
        ts = self.tick(gap)
        cur = self.conn.execute(
            "INSERT INTO events (job_id, candidate_id, type, payload, status, not_before, attempts, created_at, processed_at)"
            " VALUES (?, ?, ?, ?, 'done', ?, 0, ?, ?)",
            (job_id, cand, type_, json.dumps(payload or {}), not_before, ts, ts),
        )
        return cur.lastrowid  # type: ignore[return-value]

    def decision(self, action: str, reasoning: str, outcome: str | None = None) -> dict:
        return {"ts": iso(self.t), "action": action, "reasoning": reasoning, "outcome": outcome}


def _name_key(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


# ───────────────────────────── showcase seed ─────────────────────────────

RUBRIC = {
    "criteria": [
        {"name": "RAG systems", "weight": 0.3, "description": "Has built retrieval-augmented generation in production"},
        {"name": "TypeScript / React", "weight": 0.25, "description": "Ships product UI in TypeScript"},
        {"name": "LLM evaluation", "weight": 0.2, "description": "Measures model quality, not vibes"},
        {"name": "Ownership", "weight": 0.15, "description": "Drove a project end to end"},
        {"name": "Startup fit", "weight": 0.1, "description": "Comfortable with ambiguity and speed"},
    ],
    "interview_themes": [
        "Walk through a RAG system you built and how you evaluated retrieval",
        "A TypeScript feature you shipped end to end",
        "How you'd catch a regression in LLM output quality",
        "Why an early-stage company, why now",
    ],
}


def seed_showcase(conn: sqlite3.Connection) -> str:
    now = datetime.now(UTC)
    s = Seeder(conn, now - timedelta(minutes=60))
    job_id = ulid()
    created = s.tick(0)
    s.insert(
        "jobs",
        id=job_id,
        title="AI Engineer (RAG + TypeScript)",
        form={
            "title": "AI Engineer (RAG + TypeScript)",
            "must_haves": ["RAG", "TypeScript", "LLM evaluation"],
            "nice_to_haves": ["React", "Python"],
            "location": "Toronto / remote",
            "seniority": "mid",
            "free_text": "Early hire. Will own our retrieval stack and the product surface around it.",
        },
        rubric=RUBRIC,
        state="ACTIVE",
        shortlist_target=3,
        score_threshold=75,
        max_candidates=5,
        permission_policy={
            "restricted_actions": ["send_outreach", "send_followup", "schedule_interview", "reject_candidate_proposal"],
            "auto_approve": False,
            "auto_approve_actions": [],
            "approval_timeout_minutes": None,
        },
        agent_thread_id=f"{job_id}:job",
        created_at=created,
        updated_at=created,
    )
    s.event(job_id, None, "JobCreated", {"title": "AI Engineer (RAG + TypeScript)"}, gap=1)

    people = [
        # name, headline, final state
        ("Sarah Chen", "ML Engineer @ Cohere-backed startup", "SHORTLISTED"),
        ("Marcus Webb", "Full-stack engineer @ Shopify", "OUTREACH_DRAFTED"),
        ("Priya Natarajan", "AI Engineer @ Wealthsimple", "INTERESTED"),
        ("Diego Alvarez", "Backend engineer @ RBC", "EVALUATED"),
        ("Lena Fischer", "Frontend engineer @ Figma", "CONTACTED"),
        ("Tom Okafor", "Research engineer @ Vector Institute", "NEEDS_HUMAN"),
        ("Aisha Rahman", "Software engineer @ Ada", "INTERVIEWING"),
        ("Jonas Berg", "Staff engineer @ Spotify", "DECLINED"),
        ("Kim Lee", "New grad, WLU", "RESEARCHED"),
    ]
    ids: dict[str, str] = {}
    for i, (name, headline, _) in enumerate(people):
        cid = ulid()
        ids[name] = cid
        ts = s.tick(2)
        first = name.split()[0].lower()
        s.insert(
            "candidates",
            id=cid, job_id=job_id, ploid_id=f"ploid_{i + 1:03d}", name=name, name_key=_name_key(name),
            state="DISCOVERED", created_at=ts, updated_at=ts,
        )
        s.insert(
            "candidate_memory",
            candidate_id=cid,
            profile={
                "headline": headline,
                "skills": ["TypeScript", "React", "RAG", "Python"][: 2 + i % 3],
                "projects": [{"name": "AI Study Assistant", "description": "RAG over lecture notes"}] if first == "sarah" else [],
                "education": ["WLU"] if first in {"sarah", "kim"} else [],
                "links": {"github": f"https://github.com/{first}-dev"},
                "completeness": 0.4 if first == "kim" else 0.85,
            },
            research_notes={},
            contact={},
            contact_log=[],
            decisions=[],
            summary=f"{name}: {headline}.",
            updated_at=ts,
        )
        payload = {"ploid_id": f"ploid_{i + 1:03d}", "name": name,
                   "decision": {"action": "search_candidates", "reasoning": "Matched must-haves in Ploid search."}}
        if i == len(people) - 1:
            payload["duplicates_skipped"] = [{"name": "Sarah  Chen", "existing_candidate_id": ids["Sarah Chen"],
                                              "reason": "same GitHub profile"}]
        s.event(job_id, cid, "CandidateFound", payload, gap=2)

    def set_state(cid: str, state: str, **extra) -> None:
        sets = ", ".join(f"{k} = ?" for k in ("state", "updated_at", *extra))
        conn.execute(f"UPDATE candidates SET {sets} WHERE id = ?", (state, iso(s.t), *extra.values(), cid))

    def decide(cid: str, action: str, reasoning: str, outcome: str | None = None) -> dict:
        d = s.decision(action, reasoning, outcome)
        row = conn.execute("SELECT decisions FROM candidate_memory WHERE candidate_id = ?", (cid,)).fetchone()
        decisions = json.loads(row[0]) + [d]
        conn.execute("UPDATE candidate_memory SET decisions = ? WHERE candidate_id = ?", (json.dumps(decisions), cid))
        return {"action": action, "reasoning": reasoning}

    def enrich_and_contact(name: str, *, research: bool = False) -> None:
        cid = ids[name]
        s.event(job_id, cid, "CandidateEnriched",
                {"completeness": 0.85, "decision": decide(cid, "enrich_candidate", "New candidate; need the full profile before anything else.")}, gap=4)
        set_state(cid, "RESEARCHED")
        if research:
            s.event(job_id, cid, "CandidateEnriched",
                    {"source": "browser", "decision": decide(cid, "browser_research", "Profile completeness is 0.4; GitHub link available.")}, gap=6)
        email = f"demo+{cid[-6:].lower()}@example.com"
        conn.execute("UPDATE candidate_memory SET contact = ? WHERE candidate_id = ?",
                     (json.dumps({"email": email, "confidence": 0.9, "source": "fiber (rewritten to demo inbox)"}), cid))
        s.event(job_id, cid, "ContactFound",
                {"email": email, "decision": decide(cid, "find_contact", "Profile is complete enough; find a way to reach them.")}, gap=4)
        set_state(cid, "CONTACT_FOUND")

    def draft(name: str, subject: str, body: str) -> dict:
        cid = ids[name]
        payload = {"to": f"demo+{cid[-6:].lower()}@example.com", "subject": subject, "body": body}
        s.event(job_id, cid, "OutreachDrafted",
                {**payload, "decision": decide(cid, "draft_outreach", "Contact found; draft a personal note referencing their work.")}, gap=8)
        set_state(cid, "OUTREACH_DRAFTED")
        return payload

    def approval(name: str, action: str, payload: dict, reasoning: str, status: str = "pending",
                 decided_by: str | None = None, feedback: str | None = None) -> str:
        cid = ids[name]
        aid = ulid()
        created = s.tick(2)
        decided = s.tick(10) if status != "pending" else None
        s.insert("approvals", id=aid, job_id=job_id, candidate_id=cid, action=action, payload=payload,
                 status=status, reasoning=reasoning, decided_by=decided_by, feedback=feedback,
                 created_at=created, decided_at=decided,
                 executed_at=decided if status == "executed" else None)
        s.event(job_id, cid, "ApprovalRequested",
                {"approval_id": aid, "action": action, "decision": {"action": action, "reasoning": reasoning}}, gap=0.5)
        if status in ("approved", "executed"):
            s.event(job_id, cid, "ApprovalGranted", {"approval_id": aid, "action": action, "decided_by": decided_by}, gap=6)
        elif status == "rejected":
            s.event(job_id, cid, "ApprovalRejected", {"approval_id": aid, "action": action, "feedback": feedback}, gap=6)
        return aid

    def send(name: str, payload: dict) -> None:
        cid = ids[name]
        set_state(cid, "APPROVED")
        mid = ulid()
        ts = s.tick(3)
        s.insert("messages", id=mid, candidate_id=cid, job_id=job_id, channel="email", direction="out",
                 subject=payload["subject"], body=payload["body"], thread_id=cid, simulated=True, created_at=ts)
        conn.execute("UPDATE candidate_memory SET contact_log = ? WHERE candidate_id = ?",
                     (json.dumps([{"ts": ts, "channel": "email", "action": "send_outreach", "message_id": mid}]), cid))
        s.event(job_id, cid, "EmailSent", {"message_id": mid, "to": payload["to"], "subject": payload["subject"], "simulated": True}, gap=1)
        set_state(cid, "CONTACTED")

    def reply(name: str, body: str, disposition: str, new_state: str) -> None:
        cid = ids[name]
        ts = s.tick(15)
        s.insert("messages", id=ulid(), candidate_id=cid, job_id=job_id, channel="email", direction="in",
                 subject="Re: hello", body=body, thread_id=cid, simulated=True, created_at=ts)
        s.event(job_id, cid, "EmailReplied",
                {"body": body, "disposition": disposition,
                 "decision": decide(cid, "process_reply", f"Reply reads as {disposition.replace('_', ' ')}.")}, gap=1)
        set_state(cid, new_state)

    def interview(name: str, status: str, transcript: list | None = None) -> str:
        cid = ids[name]
        iid = ulid()
        ts = s.tick(5)
        s.insert("interviews", id=iid, candidate_id=cid, job_id=job_id, conversation_id=f"conv_{iid[-8:].lower()}",
                 join_url=f"https://elevenlabs.io/app/talk-to?agent_id=demo&conversation={iid[-8:].lower()}",
                 expires_at=iso(s.t + timedelta(hours=2)), questions=RUBRIC["interview_themes"][:3], status=status,
                 transcript=transcript or [], duration_s=312 if transcript else None, created_at=ts,
                 completed_at=s.tick(300) if status == "completed" else None)
        s.event(job_id, cid, "InterviewScheduled",
                {"interview_id": iid, "join_url": f"https://elevenlabs.io/app/talk-to?agent_id=demo&conversation={iid[-8:].lower()}"}, gap=1)
        set_state(cid, "INTERVIEWING")
        return iid

    def evaluate(name: str, score: int, rec: str, summary: str, criteria: list[dict]) -> str:
        cid = ids[name]
        eid = ulid()
        ts = s.tick(10)
        s.insert("evaluations", id=eid, candidate_id=cid, job_id=job_id, score=score, criteria=criteria,
                 recommendation=rec, summary=summary, created_at=ts)
        s.event(job_id, cid, "Evaluated",
                {"evaluation_id": eid, "score": score, "recommendation": rec,
                 "decision": decide(cid, "evaluate_candidate", "Interview transcript is in; score against the rubric.")}, gap=1)
        set_state(cid, "EVALUATED", score=score)
        return eid

    # ── Sarah: full happy path → SHORTLISTED
    enrich_and_contact("Sarah Chen")
    p = draft("Sarah Chen", "Your AI Study Assistant + our retrieval stack",
              "Hi Sarah,\n\nI saw your AI Study Assistant, RAG over lecture notes. That is close to what we're building. "
              "We're hiring our first AI engineer to own retrieval end to end. Open to a 15-minute voice chat this week?\n\nBest,\nThe CEO")
    approval("Sarah Chen", "send_outreach", p, "Strong RAG project match; personal hook from her portfolio.",
             status="executed", decided_by="ceo")
    send("Sarah Chen", p)
    reply("Sarah Chen", "Hi! Yes, this sounds great. Happy to chat this week.", "interested", "INTERESTED")
    approval("Sarah Chen", "schedule_interview", {"questions": RUBRIC["interview_themes"][:3]},
             "She replied interested; voice screen is the next step.", status="executed", decided_by="ceo")
    transcript = [
        {"role": "agent", "text": "Walk me through a RAG system you built."},
        {"role": "user", "text": "For the AI Study Assistant I chunked lecture notes by heading, used hybrid BM25 plus embeddings, and evaluated recall@5 on 200 hand-labeled questions."},
        {"role": "agent", "text": "How did you catch regressions?"},
        {"role": "user", "text": "A nightly eval set with an LLM judge, and I diffed answers whenever we changed the prompt."},
    ]
    interview("Sarah Chen", "completed", transcript)
    s.event(job_id, ids["Sarah Chen"], "InterviewDone",
            {"duration_s": 312, "decision": decide(ids["Sarah Chen"], "process_interview", "Transcript received.")}, gap=300)
    set_state(ids["Sarah Chen"], "INTERVIEWED")
    evaluate("Sarah Chen", 92, "shortlist", "Built and evaluated a real RAG system; clear on regressions; strong TypeScript.",
             [
                 {"name": "RAG systems", "weight": 0.3, "score": 95, "evidence": "Hybrid BM25 + embeddings, recall@5 on 200 labeled questions."},
                 {"name": "TypeScript / React", "weight": 0.25, "score": 90, "evidence": "AI Study Assistant frontend in Next.js."},
                 {"name": "LLM evaluation", "weight": 0.2, "score": 92, "evidence": "Nightly eval set with LLM judge; prompt diffs."},
                 {"name": "Ownership", "weight": 0.15, "score": 90, "evidence": "Solo project, end to end."},
                 {"name": "Startup fit", "weight": 0.1, "score": 85, "evidence": "Replied same day; enthusiastic."},
             ])
    s.event(job_id, ids["Sarah Chen"], "Evaluated",
            {"score": 92, "decision": decide(ids["Sarah Chen"], "shortlist", "Score 92 ≥ threshold 75.")}, gap=3)
    set_state(ids["Sarah Chen"], "SHORTLISTED")

    # ── Marcus: rejected draft with feedback → redraft → pending send_outreach
    enrich_and_contact("Marcus Webb")
    p1 = draft("Marcus Webb", "Opportunity at our company",
               "Dear Marcus,\n\nI hope this email finds you well. I am reaching out regarding an exciting opportunity at our "
               "fast-growing company, where we are building cutting-edge AI products that leverage retrieval-augmented generation...\n\nBest regards")
    approval("Marcus Webb", "send_outreach", p1, "Good TypeScript background; standard outreach.",
             status="rejected", decided_by="ceo", feedback="shorter, mention his Shopify checkout work")
    s.event(job_id, ids["Marcus Webb"], "OutreachDrafted",
            {"redraft": True, "decision": decide(ids["Marcus Webb"], "redraft_outreach", "CEO asked for shorter + a specific hook.")}, gap=8)
    p2 = {"to": p1["to"], "subject": "Your checkout work at Shopify",
          "body": "Hi Marcus, your Shopify checkout work is the kind of TypeScript we need around our RAG product. 15 minutes this week?"}
    approval("Marcus Webb", "send_outreach", p2, "Redrafted per CEO feedback: shorter, references his checkout work.")

    # ── Priya: interested → pending schedule_interview
    enrich_and_contact("Priya Natarajan")
    p = draft("Priya Natarajan", "RAG at Wealthsimple → first AI hire",
              "Hi Priya, loved your talk on retrieval for support docs. We're hiring a first AI engineer. Chat?")
    approval("Priya Natarajan", "send_outreach", p, "Direct RAG experience.", status="executed", decided_by="auto")
    send("Priya Natarajan", p)
    reply("Priya Natarajan", "Interested! What does the interview look like?", "interested", "INTERESTED")
    approval("Priya Natarajan", "schedule_interview", {"questions": RUBRIC["interview_themes"][:3]},
             "Replied interested; propose the standard 3-question voice screen.")

    # ── Diego: evaluated below threshold → pending reject_candidate_proposal
    enrich_and_contact("Diego Alvarez")
    p = draft("Diego Alvarez", "Backend + AI", "Hi Diego, we're building a RAG product and need strong backend people. Chat?")
    approval("Diego Alvarez", "send_outreach", p, "Solid backend; RAG unclear.", status="executed", decided_by="ceo")
    send("Diego Alvarez", p)
    reply("Diego Alvarez", "Sure, happy to talk.", "interested", "INTERESTED")
    approval("Diego Alvarez", "schedule_interview", {"questions": RUBRIC["interview_themes"][:3]},
             "Interested; screen to check RAG depth.", status="executed", decided_by="ceo")
    interview("Diego Alvarez", "completed", [{"role": "agent", "text": "Walk me through a RAG system you built."},
                                            {"role": "user", "text": "I haven't built one yet, but I've read about it."}])
    s.event(job_id, ids["Diego Alvarez"], "InterviewDone", {"duration_s": 240}, gap=240)
    set_state(ids["Diego Alvarez"], "INTERVIEWED")
    evaluate("Diego Alvarez", 58, "reject", "Strong backend, but no hands-on RAG or LLM evaluation.",
             [
                 {"name": "RAG systems", "weight": 0.3, "score": 30, "evidence": "\"I haven't built one yet.\""},
                 {"name": "TypeScript / React", "weight": 0.25, "score": 55, "evidence": "Mostly Java services."},
                 {"name": "LLM evaluation", "weight": 0.2, "score": 40, "evidence": "No examples given."},
                 {"name": "Ownership", "weight": 0.15, "score": 85, "evidence": "Led payments migration."},
                 {"name": "Startup fit", "weight": 0.1, "score": 70, "evidence": "Open to early stage."},
             ])
    approval("Diego Alvarez", "reject_candidate_proposal",
             {"score": 58, "reason": "Below threshold 75: no hands-on RAG or LLM evaluation.",
              "evidence": ["\"I haven't built one yet.\"", "Mostly Java services."]},
             "Score 58 < 75. Recommend a polite close.")

    # ── Lena: contacted, no reply → pending send_followup
    enrich_and_contact("Lena Fischer")
    p = draft("Lena Fischer", "Design-minded AI engineer?", "Hi Lena, your Figma plugin work caught my eye. We're hiring. Chat?")
    approval("Lena Fischer", "send_outreach", p, "Great frontend; RAG learnable.", status="executed", decided_by="ceo")
    send("Lena Fischer", p)
    approval("Lena Fischer", "send_followup",
             {"to": p["to"], "subject": "Re: Design-minded AI engineer?", "body": "Hi Lena, just bumping this in case it got buried. Worth a 15-minute chat?"},
             "No reply after the follow-up window; one nudge allowed.")

    # ── Tom: Fiber failed → NEEDS_HUMAN
    cid = ids["Tom Okafor"]
    s.event(job_id, cid, "CandidateEnriched",
            {"decision": decide(cid, "enrich_candidate", "New candidate; fetch profile.")}, gap=4)
    set_state(cid, "RESEARCHED")
    s.event(job_id, cid, "ToolFailed", {"tool": "fiber", "error_kind": "transient", "attempts": 3,
                                        "error": "Fiber API timed out after 3 retries"}, gap=10)
    trace = "https://cloud.langfuse.com/project/demo/traces/seed-trace-tom"
    s.event(job_id, cid, "Escalated", {"error": "find_contact failed: Fiber API timed out after 3 retries",
                                       "prev_state": "RESEARCHED", "trace_url": trace,
                                       "decision": decide(cid, "escalate", "Retries exhausted.")}, gap=1)
    set_state(cid, "NEEDS_HUMAN", prev_state="RESEARCHED", trace_url=trace)

    # ── Aisha: interview live
    enrich_and_contact("Aisha Rahman")
    p = draft("Aisha Rahman", "Chatbots at Ada → RAG here", "Hi Aisha, your conversational AI work at Ada is a great fit. Chat?")
    approval("Aisha Rahman", "send_outreach", p, "Conversational AI background.", status="executed", decided_by="ceo")
    send("Aisha Rahman", p)
    reply("Aisha Rahman", "Yes please!", "interested", "INTERESTED")
    approval("Aisha Rahman", "schedule_interview", {"questions": RUBRIC["interview_themes"][:3]},
             "Interested; schedule the voice screen.", status="executed", decided_by="ceo")
    interview("Aisha Rahman", "scheduled")

    # ── Jonas: declined
    enrich_and_contact("Jonas Berg")
    p = draft("Jonas Berg", "Staff-level AI role", "Hi Jonas, we'd love someone with your scale experience. Chat?")
    approval("Jonas Berg", "send_outreach", p, "Senior; long shot.", status="executed", decided_by="ceo")
    send("Jonas Berg", p)
    reply("Jonas Berg", "Thanks, but I'm happy where I am.", "not_interested", "DECLINED")

    # ── Kim: thin profile → browser research
    cid = ids["Kim Lee"]
    s.event(job_id, cid, "CandidateEnriched",
            {"completeness": 0.4, "decision": decide(cid, "enrich_candidate", "New candidate; fetch profile.")}, gap=4)
    set_state(cid, "RESEARCHED")
    s.event(job_id, cid, "CandidateEnriched",
            {"source": "browser", "decision": decide(cid, "browser_research", "Profile completeness is 0.4; GitHub link available.")}, gap=6)

    # Pinned dispositions for anyone not yet replied
    for name, disp in (("Marcus Webb", "interested"), ("Lena Fischer", "no_reply")):
        conn.execute("UPDATE candidates SET demo_disposition = ? WHERE id = ?", (disp, ids[name]))

    conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (iso(s.t), job_id))
    conn.commit()
    return job_id


def reset_demo(seed: bool = True, path: Path | None = None) -> str | None:
    conn = reset_db(path)
    try:
        return seed_showcase(conn) if seed else None
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--empty", action="store_true", help="wipe without seeding")
    args = ap.parse_args()
    t0 = time.perf_counter()
    job_id = reset_demo(seed=not args.empty)
    print(f"[reset_demo] {db_path()} reset in {time.perf_counter() - t0:.2f}s" + (f"; job {job_id}" if job_id else ""))
    if job_id:
        url = os.environ.get("WEB_URL", "http://localhost:3000")
        print(f"[reset_demo] open {url}/jobs/{job_id}")


if __name__ == "__main__":
    main()
