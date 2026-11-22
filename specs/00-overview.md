# 00 — Overview

## One-line pitch
A persistent recruiter agent that maintains the state of every candidate, chooses the next recruiting action, and executes it through specialized tools, with permission boundaries around real-world actions.

Not "we have several AI agents". One agent brain, deterministic control around it.

## Goals (4-hour hackathon build)
1. CEO creates a role. The agent sources, researches, contacts, interviews (voice), evaluates and shortlists candidates, with minimal CEO input.
2. The LLM decides *what to do next*. Application code decides *what is allowed* (state machine + permission layer).
3. Every real-world action is gated: the CEO approves or auto-approve is on.
4. Persistent: state survives restarts, and the agent never forgets who it contacted (shared Candidate Memory).
5. Observable: every run is traced in Langfuse; failures surface in the dashboard with a trace link.

## Non-goals
- Real outbound email or real third-party candidate contact (all outbound is **simulated** in the demo).
- Multi-tenant auth, billing, ATS integrations, scheduling with calendars.
- More than ~5 candidates per role in the demo (the design allows more).

## Architecture (hybrid: Central Agent + State Machine + Permission Layer + Events + Memory)

```
                          CEO (Next.js dashboard)
                                  │  create role / approve / reject
                                  ▼
                       ┌────────────────────┐
                       │   FastAPI service  │  (hosted on Maritime)
                       └─────────┬──────────┘
                                 │ events
                                 ▼
   ┌────────────────────────────────────────────────────────────┐
   │  RECRUITER AGENT (LangGraph, one instance per role)        │
   │                                                            │
   │  observe ─► decide (Claude) ─► permission_check ─► execute │
   │     ▲                               │ restricted?          │
   │     │                               ▼                      │
   │     │                       approval queue (CEO)           │
   │     └──────── update_state ◄── tool result ◄──────────────┘
   └───────┬──────────────┬──────────────┬──────────────┬───────┘
           ▼              ▼              ▼              ▼
         Ploid          Fiber         Browser       ElevenLabs
       (discover/     (contact      (public-page    (voice
        enrich)        lookup)       research)      interview)
           └──────────────┴──────┬───────┴──────────────┘
                                 ▼
                    Candidate Memory (SQLite, blackboard)
                                 │
                                 ▼
                     Event log ──► wakes the agent
                                 │
                       Langfuse traces everything
```

### The core loop
```
Event arrives (e.g. CandidateFound)
  → load candidate state + memory
  → decide: LLM picks ONE next action from the actions valid for the current state
  → permission check: auto-allowed, or create an approval and pause
  → execute the tool
  → update state + memory
  → emit event (e.g. CandidateEnriched) → agent wakes again
```
The agent keeps asking: *"Given the current state, what's the best next action?"*

## Component summary
| Component | Responsibility | Spec |
|---|---|---|
| State machine | Valid states, transitions, valid actions per state | [01](01-state-machine.md) |
| Tool adapters | Ploid, Fiber, browser, email sim, ElevenLabs, candidate persona | [02](02-tools-and-adapters.md) |
| Permission layer | READ / WRITE / RESTRICTED, approvals, auto-approve | [03](03-permissions.md) |
| Data model | SQLite schema, memory, event log, duplicate guard | [04](04-data-model.md) |
| Agent graph | LangGraph nodes, event dispatcher, Langfuse | [05](05-agent-graph.md) |
| API + dashboard | FastAPI endpoints, CEO UI | [06](06-api-and-dashboard.md) |
| Deployment | Maritime, Docker, AgentRunner | [07](07-deployment-maritime.md) |
| Build plan | Timeline, cut lines, demo script, risks | [08](08-build-plan.md) |

## Key decisions
- **Stack:** Python, LangGraph, FastAPI, SQLite (+ LangGraph SQLite checkpointer), Next.js dashboard.
- **LLMs:** Claude for reasoning and evaluation, ElevenLabs Conversational AI for voice.
- **Hosting:** Maritime hosts the Python service. LangGraph orchestrates the agent inside it. One recruiter agent instance per role.
- **Observability:** Langfuse traces every graph run. Failure path: retry, then `NEEDS_HUMAN` with a trace link.
- **Mock-first:** every external tool is an adapter with a mock implementation, so the demo never depends on a third-party outage.

## Glossary
- **Candidate state:** one of the lifecycle values in [01](01-state-machine.md).
- **Action:** a named operation the agent can choose (e.g. `find_contact`). Each action maps to a tool and a permission class.
- **Restricted action:** requires CEO approval unless auto-approve is on for the role.
- **Candidate Memory:** the single source of truth for everything known about a candidate (profile, contact, conversations, interviews, decisions).
- **Event:** a durable row in the `events` table that wakes the agent for a given job and candidate.

## Open items (confirm at the hackathon)
| Item | Status | Fallback |
|---|---|---|
| Ploid API endpoints, auth, rate limits | TBD | Mock adapter with fixture candidates |
| Fiber API endpoints, auth | TBD | Mock adapter returning demo-inbox addresses |
| Maritime deploy interface (CLI/SDK/dashboard) | TBD | Run locally via the same Docker image |
| Maritime public webhook URL for ElevenLabs | TBD | Poll the ElevenLabs conversation API |
| Browser tool implementation (Playwright on Maritime vs. Maritime-provided) | TBD | Disable and rely on Ploid data |
| ElevenLabs agent ID/voice and post-call webhook format | TBD | Poll for the transcript |
