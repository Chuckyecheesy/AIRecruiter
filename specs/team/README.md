# Team Split: 3 people, no stepping on each other

The build is split into three **lanes**. Each lane owns a set of files. **You only edit files in your own lane.** If you need something from another lane, ask the owner, or code against the shared contract and a stub.

| Lane | Person | Focus | Checklist |
|---|---|---|---|
| **A: Agent Core** | _name_ | State machine, DB, events, dispatcher, LangGraph, permissions, Langfuse | [person-a-agent-core.md](person-a-agent-core.md) |
| **B: Tools & Infra** | _name_ | Adapters (Ploid, Fiber, browser, email sim, persona, ElevenLabs), Claude drafter/evaluator/rubric, Docker, Maritime | [person-b-tools-infra.md](person-b-tools-infra.md) |
| **C: API & Dashboard** | _name_ | FastAPI routes, assistant-ui chat dashboard (Next.js), SSE, demo scripts, demo rehearsal | [person-c-api-dashboard.md](person-c-api-dashboard.md) |

## File ownership map

Only the owner edits a file. Everyone can read every file.

```
AIRecruiter/
  api/
    contracts.py           ★ SHARED: written together at 0:00, then frozen (A merges changes)
    config.py              B   (all env vars / settings)
    main.py                C   (app factory, router includes, calls A's start_dispatcher())
    agent/                 A   graph, nodes, state_machine, permissions, dispatcher, prompts
    db/schema.sql          A
    db/repo.py             A   (all writes + agent reads)
    db/queries.py          C   (read-only queries for the dashboard)
    observability/         A   langfuse.py
    tools/                 B   base, ploid, fiber, browser, email_sim, elevenlabs, persona,
                               drafter, evaluator, rubric
    routes/webhooks.py     B   (ElevenLabs webhook)
    routes/*.py (others)   C   jobs, candidates, approvals, events, chat, admin, health
  web/                     C   Next.js + assistant-ui (Thread, tool UIs, external store runtime)
  fixtures/                B
  scripts/reset_demo.py    C
  scripts/smoke.py         C
  tests/agent/             A
  tests/tools/             B
  tests/api/               C
  Dockerfile, docker-compose.yml, .env.example      B
  Makefile, README.md                               C
  specs/                   anyone (small edits; announce in chat)
```

## Boundary rules (these keep the lanes from blocking each other)

1. **Tools never touch the DB.** B's adapters take Pydantic inputs and return Pydantic outputs, or raise `ToolError`. A's `execute`/`update_state` persist everything (`messages`, `contact_log`, `interviews`, `evaluations`, delayed persona `EmailReplied` events). *(This differs from spec 02, where EmailSimulator writes the `messages` row. Moving that into A's lane keeps B off the DB.)*
2. **Routes never change state directly.** C's routes call A's repo functions (`emit_event`, `decide_approval`, `set_job_state`, ...) for writes. For reads, C writes its own queries in `db/queries.py`.
3. **The contract is `api/contracts.py` plus `web/lib/types.ts`.** It holds the enums (`CandidateState`, `JobState`, `EventType`, `ActionName`, `PermissionClass`), the tool I/O models from spec 02, and the API response shapes from spec 06. Changing it after 0:20 means posting in chat first. A merges the change, and C mirrors it in `types.ts`.
4. **Stub everything you depend on.** A runs with `TOOLS_MODE=mock` + `LLM_MODE=stub`. C builds the UI against data seeded by `reset_demo.py` before the agent works. B tests adapters in isolation with `tests/tools/`.
5. **Git:** one branch per lane (`lane-a/*`, `lane-b/*`, `lane-c/*`). Rebase on `main` and merge **small PRs often** (at least at every sync point below). Since nobody shares files, merges should be conflict-free.

## The three handoff interfaces

| From → To | Interface | Defined in |
|---|---|---|
| B → A | `get_tools() -> Tools` (a dataclass holding every adapter, mock or real based on config) | `api/tools/__init__.py` |
| A → C | `repo.py` write functions + `start_dispatcher()` + the events table | `api/db/repo.py`, `api/agent/dispatcher.py` |
| B → C | `RubricGenerator.generate(form)`, `tools_status()` for `/health`, webhook router | `api/tools/`, `api/routes/webhooks.py` |

## Sync points (all three people, ~5 min each)

| Time | Sync | Exit criteria |
|---|---|---|
| **0:00-0:20** | Kickoff: write `contracts.py` + `schema.sql` together | Contract merged to `main`, schema applied, every lane can start alone |
| **1:10** | Core loop | Mock job reaches `OUTREACH_DRAFTED` for all candidates (A + B) |
| **1:40** | Approvals | Approve in API → `CONTACTED` → persona reply → `INTERESTED` (A + B + C) |
| **2:20** | Visual demo | Whole mock flow is visible in the dashboard (C) |
| **2:50** | Voice + eval | A real ElevenLabs interview ends in `SHORTLISTED` (B + A + C) |
| **3:20** | Deployed | Running on Maritime, or decision made to demo locally (B) |
| **3:40** | Reliability | `NEEDS_HUMAN` + trace link, redraft, duplicate guard all work (A) |
| **3:40-4:00** | Demo prep | Everyone rehearses together (C leads) |

## Acceptance criteria owners (from spec 08)

| Criterion | Owner |
|---|---|
| A role created in the dashboard produces ≥3 candidates that progress with no manual API calls | C (verifies end to end) |
| No restricted action executes without an `approved` approval (test) | A |
| A CEO rejection with feedback causes a visibly different redraft | A + B |
| A server restart mid-run loses no state and resumes pending events | A |
| At least one real voice interview completes and is evaluated | B |
| A forced tool failure ends in `NEEDS_HUMAN` with a working trace link | A (B provides `FAIL_TOOL`) |
| The duplicate candidate is skipped and shown in the log | A (B provides the fixture) |
| `TOOLS_MODE=mock` runs the whole flow offline except Claude | B |
