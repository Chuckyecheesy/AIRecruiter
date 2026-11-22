# 08 — Build Plan (4 hours)

## Principles
- **Vertical slice first:** get one candidate through the whole lifecycle in mock mode, then swap in real tools one at a time.
- **Mock-first:** the demo must work with every external tool mocked except Claude and (ideally) ElevenLabs.
- **Cut lines are explicit:** anything below the line is dropped without guilt.

## Timeline

| Time | Milestone | Deliverable |
|---|---|---|
| 0:00-0:20 | Skeleton | Repo layout, FastAPI app, SQLite schema (`04`), config and env, mock fixtures, `.env.example` |
| 0:20-1:10 | **Core loop** | State machine + `valid_actions`, event table + dispatcher, LangGraph nodes (`05`), Claude decide, Ploid/Fiber mocks, Drafter. **Checkpoint:** a job reaches `OUTREACH_DRAFTED` for all candidates |
| 1:10-1:40 | Permission layer | `permission_check`, approvals table and API, execute-from-approval, email simulator, persona replies. **Checkpoint:** approve → `CONTACTED` → `INTERESTED` |
| 1:40-2:20 | Dashboard v1 | Role form, pipeline kanban, approval queue, activity log via SSE. **Checkpoint:** the demo flow works visually |
| 2:20-2:50 | Voice + evaluation | ElevenLabs session + webhook (or poll), `Evaluator`, shortlist and reject proposals. **Checkpoint:** a real voice interview ends in `SHORTLISTED` |
| 2:50-3:20 | Real tools and Maritime | Ploid/Fiber real adapters (if the APIs are confirmed), Dockerfile, deploy to Maritime |
| 3:20-3:40 | Reliability + Langfuse | Tracing, retry then `NEEDS_HUMAN` with trace link, duplicate guard, CEO-rejects-draft redraft |
| 3:40-4:00 | Demo prep | `reset_demo.py`, pinned dispositions, rehearsal, a fallback recording if live fails |

## Priorities and cut lines

**Must have (above the line):**
1. Mock-mode end-to-end lifecycle through to `SHORTLISTED`.
2. Permission layer with the approval queue and the auto-approve toggle.
3. Dashboard: role form, pipeline, approval queue, activity log.
4. Persistent state (SQLite) and the event-driven loop.
5. Real ElevenLabs voice interview.
6. Langfuse tracing and the `NEEDS_HUMAN` + trace-link path.

**Should have:**
7. Real Ploid and/or Fiber.
8. Deployed on Maritime.
9. CEO-rejects-draft re-plan; duplicate guard visible in the demo; decline/stale path.

**Stretch (cut first):**
10. Browser research tool.
11. Second job running in parallel.
12. `MaritimeRunner` (one VM per recruiter).
13. Candidate drawer polish, shortlist export.

## Suggested repo layout
```
AIRecruiter/
  specs/                 # these documents
  api/                   # see 06 for the structure
  web/                   # Next.js dashboard
  fixtures/              # ploid_candidates.json, fiber_contacts.json, scripted transcript
  scripts/               # reset_demo.py, smoke.py
  Dockerfile  docker-compose.yml  Makefile  .env.example  README.md
```

## Parallelization (if 2+ people)
- **Person A (backend/agent):** `04` → `01` → `05` → `03` → tools.
- **Person B (frontend/demo):** `06` UI against the API contract using a stub server first, then wire up SSE; also own ElevenLabs agent setup and the demo script.
- The contract is the types in `06` and the event names in `04`. Agree on those in the first 20 minutes.

## Demo script (≈4 minutes)

1. **Set the scene (20s):** "A persistent recruiter agent. It decides, we control what's allowed."
2. **Create the role (30s):** fill in the form (e.g. "AI Engineer, RAG + TypeScript"); show the generated rubric. Leave auto-approve **off**.
3. **Agent works (45s):** candidates appear in `DISCOVERED` and move to `RESEARCHED` and `CONTACT_FOUND`. The activity log shows the agent's reasoning at each step. *Moment:* a duplicate candidate is skipped ("already in pipeline").
4. **Permission layer (45s):** the approval queue shows a draft email. **Reject with feedback** ("shorter, mention the AI study assistant") → the agent redrafts → **approve**. Say: "The agent can reason on its own but can't contact anyone without approval."
5. **Events (30s):** the persona replies (simulated). One candidate declines, and the others move to `INTERESTED`. The agent proposes interviews → approve.
6. **Voice interview (60s):** open the join link, and the team member answers 2-3 questions live. The transcript lands and the candidate moves to `INTERVIEWED` → `EVALUATED`, with a score and per-criterion evidence.
7. **Shortlist (20s):** show the ranked shortlist. The agent proposes a rejection for a low scorer → approve.
8. **Resilience (optional, 20s):** force a tool failure on one candidate (`FAIL_TOOL=fiber` demo flag) → retries → `NEEDS_HUMAN` with a Langfuse trace link → Resume.
9. **Close (10s):** "Flip auto-approve and the same agent runs hands-free, with the same audit trail."

Pre-demo: `python scripts/reset_demo.py`, pin dispositions (`interested`, `interested`, `not_interested`, `no_reply`, `interested`), warm the ElevenLabs session, and have a screen recording as a fallback.

## Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Ploid/Fiber API unclear or rate-limited | High | Mock-first adapters; fixtures include a duplicate and a thin profile |
| Maritime deploy friction | Medium | Plain Docker image; run the demo locally if deploy fails by 3:30; `InProcessRunner` only |
| ElevenLabs webhook unreachable | Medium | `ELEVENLABS_POLL=true`; scripted mock transcript as a last-resort fallback |
| LLM returns an invalid action | Medium | Structured output + validation + one re-decide + escalate |
| SQLite lock contention | Low | WAL mode; checkpoints in a separate file; the dispatcher serializes per candidate |
| Demo latency (too many LLM calls) | Medium | `PERSONA_DELAY_S=8`, a cheaper model for persona/draft, auto-approve for the non-judged parts |
| Real email sent by accident | Low (high impact) | Sender is simulated; the real path needs `ALLOW_REAL_EMAIL` and an allowlist; Fiber output is rewritten to `DEMO_INBOX` |
| Langfuse keys missing | Low | Tracing is a no-op; the app runs without it |

## Acceptance criteria (definition of done)
- [ ] A role created in the dashboard produces ≥3 candidates that progress without manual API calls.
- [ ] No restricted action executes without an `approved` approval row (verified by a test attempting a direct call).
- [ ] A CEO rejection with feedback causes a visibly different redraft.
- [ ] Restarting the server mid-run loses no candidate state and resumes pending events.
- [ ] At least one real voice interview completes and is evaluated.
- [ ] A forced tool failure ends in `NEEDS_HUMAN` with a working trace link (or a clear message if Langfuse is not configured).
- [ ] The duplicate candidate is skipped and shown in the log.
- [ ] `TOOLS_MODE=mock` runs the entire flow offline except for Claude.
