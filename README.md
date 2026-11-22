# AIRecruiter

A persistent recruiter agent: it keeps the state of every candidate, uses Claude to choose the next action, and runs it through tools. Code decides what is allowed (state machine + permission layer); the CEO approves anything that contacts, interviews or rejects someone.

Specs: [`specs/`](specs/). Team lanes: [`specs/team/`](specs/team/README.md).

## Run locally

```bash
py -3.12 -m venv .venv            # or python3.12 -m venv .venv
make install                      # API deps + web deps
cp .env.example .env              # ANTHROPIC_API_KEY at minimum (lane B ships this file)
python scripts/reset_demo.py      # wipe the DB and seed a showcase job
TOOLS_MODE=mock make dev          # API on :8000, dashboard on :3000
```

Without `make` (Windows): run `uvicorn api.main:app --reload --port 8000` and `cd web && npm run dev` in two terminals.

`reset_demo.py` prints the job URL. Open it to see the candidates and the activity feed.

### Useful env vars (lane C)

| Var | Purpose |
|---|---|
| `NEXT_PUBLIC_API_URL` | Dashboard → API base URL (default `http://localhost:8000`) |
| `CORS_ORIGINS` | Comma-separated dashboard origins allowed by the API (default `http://localhost:3000`) |
| `DEMO_TOKEN` / `NEXT_PUBLIC_DEMO_TOKEN` | Optional shared token for the deployed instance (`X-Demo-Token` header, `?token=` on the SSE stream) |
| `ADMIN_ENABLED` | Enables `POST /api/admin/reset` |
| `RECRUITER_DB` | Override the DB path (default `$DATA_DIR/recruiter.db`) |
| `DISPATCHER_ENABLED=false` | Start the API without the agent loop |

## Tests

```bash
python -m pytest tests/api
```

End-to-end smoke run (in-process app, temp DB, mock tools, stub LLM). Creates a role the way the dashboard does, turns auto-approve on, starts it, then only watches:

```bash
python scripts/smoke.py                          # waits for a SHORTLISTED candidate
python scripts/smoke.py --until OUTREACH_DRAFTED # stop at an earlier stage
python scripts/smoke.py --url http://localhost:8000  # against a running server
```

## Dashboard

- `/`: new role form, then rubric review, then Start recruiter.
- `/jobs/[id]`: the candidate list, with anyone who needs you (pending approval or `NEEDS_HUMAN`) on top and their approval card inline (edit, Approve, Reject with feedback). A read-only activity feed on the right shows what the agent did and why. Pause/Resume are in the header.
- `/jobs/[id]/shortlist`: ranked shortlist with per-criterion evidence and Copy as Markdown.
