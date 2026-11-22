# 04 — Data Model

SQLite (single file `data/recruiter.db`, WAL mode). Application tables live in this file. The LangGraph SQLite checkpointer uses its own file (`data/checkpoints.db`) to avoid lock contention.

Conventions: IDs are ULIDs (text). Timestamps are ISO-8601 UTC text. JSON columns are text validated by Pydantic.

## Tables

### `jobs`
| Column | Type | Notes |
|---|---|---|
| id | text PK | |
| title | text | |
| form | json | must_haves, nice_to_haves, location, seniority, free_text |
| rubric | json | criteria with weights, interview themes (see [02](02-tools-and-adapters.md)) |
| state | text | `DRAFT`/`ACTIVE`/`PAUSED`/`COMPLETE` |
| shortlist_target | int | default 3 |
| score_threshold | int | default 75 |
| max_candidates | int | default 5 |
| permission_policy | json | see [03](03-permissions.md) |
| agent_thread_id | text | LangGraph thread (one per job) |
| created_at, updated_at | text | |

### `candidates`
| Column | Type | Notes |
|---|---|---|
| id | text PK | |
| job_id | text FK | |
| ploid_id | text | unique with job_id |
| name | text | |
| name_key | text | normalized for the duplicate guard |
| state | text | lifecycle state ([01](01-state-machine.md)) |
| prev_state | text | set when moving to `NEEDS_HUMAN` |
| score | int null | latest evaluation score |
| demo_disposition | text null | pins the persona reply for scripted demos |
| locked_by, locked_at | text | per-candidate processing lock |
| trace_url | text null | Langfuse link for the last failure |
| created_at, updated_at | text | |
UNIQUE(job_id, ploid_id), INDEX(job_id, state).

### `candidate_memory` (blackboard, one row per candidate)
| Column | Type | Notes |
|---|---|---|
| candidate_id | text PK | |
| profile | json | skills, projects, education, experience, links, completeness |
| research_notes | json | browser findings and agent notes |
| contact | json | email, phone, confidence, source (the demo email is rewritten to the inbox) |
| contact_log | json | list of `{ts, channel, action, message_id}`: **"did we already contact this person?"** |
| decisions | json | list of `{ts, action, reasoning, outcome}` |
| summary | text | rolling LLM-written summary for the decide prompt (keeps prompts short) |
| updated_at | text | |

Example (the single source of truth for one candidate):
```
Sarah
├── skills: React, TypeScript, RAG
├── education: WLU
├── projects: AI Study Assistant
├── contact_log: emailed Nov 22
├── conversation: responded "interested"
├── interview: complete (transcript id c_123)
├── evaluation: 92
└── decisions: [enrich, find_contact, draft, send (approved by CEO), schedule, evaluate, shortlist]
```
All tools write here. `decide` reads here. Nothing is passed between steps except through memory.

### `messages`
| Column | Notes |
|---|---|
| id, candidate_id, job_id | |
| channel | `email` / `voice` |
| direction | `out` / `in` |
| subject, body | |
| thread_id | per candidate |
| simulated | bool (always true for email in the demo) |
| created_at | |

### `interviews`
| Column | Notes |
|---|---|
| id, candidate_id, job_id | |
| conversation_id | ElevenLabs |
| join_url, expires_at | |
| questions | json |
| status | `scheduled` / `in_progress` / `completed` / `expired` |
| transcript | json (turns) |
| duration_s | |
| created_at, completed_at | |

### `evaluations`
| Column | Notes |
|---|---|
| id, candidate_id, job_id | |
| score | 0-100 |
| criteria | json (name, weight, score, evidence) |
| recommendation | `shortlist`/`reject`/`borderline` |
| summary | text |
| created_at | |

### `approvals`
Fields as in [03](03-permissions.md): id, job_id, candidate_id, action, payload (json), status, reasoning, decided_by, feedback, created_at, decided_at, executed_at. INDEX(job_id, status).

### `events` (durable bus + audit log)
| Column | Notes |
|---|---|
| id | autoincrement (the ordering cursor) |
| job_id, candidate_id (null for job-level) | |
| type | e.g. `CandidateFound` |
| payload | json |
| status | `pending` / `processing` / `done` / `failed` |
| not_before | text null (delayed events, e.g. persona replies, ticks) |
| attempts | int |
| created_at, processed_at | |
INDEX(status, not_before).

Event types: `JobCreated`, `CandidateFound`, `CandidateEnriched`, `ContactFound`, `OutreachDrafted`, `ApprovalRequested`, `ApprovalGranted`, `ApprovalRejected`, `EmailSent`, `EmailReplied`, `InterviewScheduled`, `InterviewDone`, `Evaluated`, `Tick`, `ToolFailed`, `Escalated`, `HumanResolved`.

### `tool_calls`
id, run_id, candidate_id, tool, action, idempotency_key (unique), input (json), output (json), status (`ok`/`error`), error_kind, attempts, latency_ms, langfuse_span_id, created_at.

### `agent_runs`
id, job_id, candidate_id, event_id, langfuse_trace_id, trace_url, status (`running`/`ok`/`error`), chosen_action, error, started_at, ended_at. One row per graph invocation.

## Duplicate guard
1. On `search_candidates`, each result is checked before insert: exact `(job_id, ploid_id)` match, or `name_key` match (lowercase, whitespace/punctuation stripped) plus any shared profile URL.
2. A match is not inserted. A `decisions` note is added to the existing candidate's memory ("duplicate found, skipped"), and a `CandidateFound` is *not* emitted.
3. Before `send_outreach`/`send_followup`, the `contact_log` is checked for an identical recent send. `send_outreach` is refused if one already exists (the refusal is shown in the activity log).

## LangGraph checkpointer
- `SqliteSaver` (or `AsyncSqliteSaver`) on `data/checkpoints.db`.
- `thread_id = f"{job_id}:{candidate_id}"` for per-candidate graph runs, and `f"{job_id}:job"` for job-level runs. This gives each candidate its own durable thread, so an approval pause resumes exactly where it left off.
- Application tables remain the source of truth for state. The checkpoint only holds in-flight graph state (e.g. a pending decision awaiting approval).

## Seeding and reset
`scripts/reset_demo.py` drops and recreates the DB, then optionally seeds a demo job with a pinned persona disposition for each candidate.
