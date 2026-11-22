# AIRecruiter

A persistent recruiter agent (4-hour hackathon build). It keeps the state of every candidate, uses an LLM to choose the next action, and runs that action through tools. Application code decides what is *allowed*: a state machine plus a permission layer. One agent brain, deterministic control around it.

Full specs are in `specs/` (`00`–`08`). The team split and per-person checklists are in `specs/team/`. **Read the relevant spec before changing a component.**

## Stack
- **Backend:** Python 3.12, FastAPI, LangGraph, SQLite (WAL) + LangGraph `SqliteSaver` (separate file), Pydantic
- **LLM:** Claude `claude-sonnet-5-5` (decide, draft, persona, evaluate, rubric); model IDs come from config (`MODEL_*`)
- **Voice:** ElevenLabs Conversational AI. **Tracing:** Langfuse. **Hosting:** Maritime (Docker)
- **Frontend:** Next.js App Router + Tailwind + shadcn + **assistant-ui** (external store runtime fed by SSE events; approvals render as tool UI cards)

## Layout
```
api/contracts.py   shared enums + Pydantic models (frozen after kickoff; changes go through lane A)
api/agent/         graph, nodes, state_machine, permissions, dispatcher, prompts     [lane A]
api/db/            schema.sql, repo.py (writes) [A] · queries.py (dashboard reads) [C]
api/observability/ langfuse.py                                                      [A]
api/tools/         adapters + drafter/evaluator/rubric/persona; config.py           [B]
api/routes/        webhooks.py [B] · everything else [C];  api/main.py [C]
web/               assistant-ui dashboard                                           [C]
fixtures/ [B]   scripts/ (reset_demo.py, smoke.py) [C]   Dockerfile, .env.example [B]
```
**Only edit files in your own lane** (see `specs/team/README.md`).

## Core loop
Event row → `observe` (lock, load memory, `valid_actions`) → `decide` (Claude picks ONE action from the valid list) → `permission_check` → `execute` (tool) → `update_state` (`apply_transition`, write memory, emit next event). One graph invocation = one event for one candidate.

## Invariants (do not break)
- The LLM only sees `valid_actions(state)`. An invalid action gets one re-decide, then `escalate`. **Only code applies transitions.**
- RESTRICTED actions (`send_outreach`, `send_followup`, `schedule_interview`, `reject_candidate_proposal`) run only from an `approved` approval row, using **its stored payload**. Restricted tools re-check this themselves. An approval executes at most once.
- `reject_candidate_proposal` is never auto-approved unless it is listed explicitly.
- **No real email, ever:** the email sender is simulated. A real send requires `ALLOW_REAL_EMAIL=true` + `EMAIL_ALLOWLIST`. Contact emails are rewritten to `DEMO_INBOX+{id}`.
- **Tools never touch the DB.** They take and return Pydantic models or raise `ToolError(kind)`. Lane A persists results.
- **Routes never write tables directly.** They call `repo.py` functions.
- Candidate Memory (`candidate_memory`) is the single source of truth. Steps communicate only through memory and events.
- Every external tool has a mock (`TOOLS_MODE=mock`). The app must run without Langfuse keys (tracing becomes a no-op).
- Tool failure: retry per the taxonomy (`transient`/`rate_limited`/`not_found`/`invalid_input`/`fatal`), then `NEEDS_HUMAN` with `trace_url`.

## Conventions
- IDs are ULIDs; timestamps are ISO-8601 UTC text; JSON columns are validated by Pydantic.
- All adapter methods are `async`.
- Event and state names must match `api/contracts.py` exactly (mirrored in `web/lib/types.ts`).

## Commands
```
cp .env.example .env              # GEMINI_API_KEY at minimum
TOOLS_MODE=mock make dev          # api :8000, web :3000
LLM_MODE=stub                     # canned decisions, offline
python scripts/reset_demo.py      # reset + seed demo job
python scripts/smoke.py           # end-to-end mock run → SHORTLISTED
pytest                            # tests/{agent,tools,api}
```
