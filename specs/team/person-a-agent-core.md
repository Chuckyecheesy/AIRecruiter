# Lane A: Agent Core

**You own:** `api/agent/`, `api/db/schema.sql`, `api/db/repo.py`, `api/observability/`, `tests/agent/`. You also merge changes to `api/contracts.py`.
**You don't touch:** `api/tools/`, `api/routes/`, `api/main.py`, `web/`, `fixtures/`, `config.py`, Docker files.
**Read these specs:** [01 State Machine](../01-state-machine.md), [03 Permissions](../03-permissions.md), [04 Data Model](../04-data-model.md), [05 Agent Graph](../05-agent-graph.md)

**You depend on:** B's `get_tools()` (start with your own inline fakes until B's mocks merge, around 0:40).
**Others depend on you:** C needs `repo.py` write functions and `start_dispatcher()`. Ship signatures early, even as stubs.

---

## 0:00-0:20 Kickoff (with everyone)
- [ ] Write the `api/contracts.py` enums: `CandidateState`, `JobState`, `EventType` (all 17 from spec 04), `ActionName`, `PermissionClass`
- [ ] Write the `Decision`, `Approval`, `Event`, `CandidateMemory` Pydantic models in `contracts.py`
- [ ] `db/schema.sql`: all tables from spec 04 (jobs, candidates, candidate_memory, messages, interviews, evaluations, approvals, events, tool_calls, agent_runs), WAL mode, indexes
- [ ] `repo.py` **signatures only** (bodies can be `...`) so C can import them: `create_job`, `update_job`, `set_job_state`, `emit_event`, `decide_approval(id, approved, payload|feedback)`, `resume_candidate`, `skip_candidate`, `set_demo_disposition`
- [ ] `agent/dispatcher.py` exports `start_dispatcher()` / `stop_dispatcher()` (can be no-ops at first)
- [ ] Merge to `main`

## 0:20-1:10 Core loop → checkpoint: mock job reaches `OUTREACH_DRAFTED`
- [ ] `state_machine.py`: transition table from spec 01, `valid_actions(state, job)`, `apply_transition(state, action, result)`
- [ ] Unit tests for every row of the transition table
- [ ] `repo.py` real implementations (ULIDs, ISO timestamps, JSON columns validated with Pydantic)
- [ ] Per-candidate lock (`locked_by`, `locked_at`)
- [ ] Dispatcher: 1s poll loop, respects `not_before`, serial per candidate, `Semaphore(MAX_CONCURRENT_RUNS)`, skips `PAUSED` jobs
- [x] Crash recovery: reset `processing` rows older than 2 min to `pending` on startup
- [x] LangGraph `StateGraph`: `observe → decide → permission_check → execute → update_state`, plus `escalate`
- [x] `SqliteSaver` on `data/checkpoints.db`, thread id `{job_id}:{candidate_id}` / `{job_id}:job`
- [x] `prompts.py`: decide prompt contract from spec 05, structured output `{action, args, reasoning}`
- [x] Decide validation: action must be in `valid_actions`. Re-decide once, then escalate
- [x] `LLM_MODE=stub`: canned happy-path decisions (also unblocks C's UI work)
- [x] `JobCreated` → `search_candidates` → insert `DISCOVERED` up to `max_candidates` → emit `CandidateFound` per candidate
- [x] `execute` calls `get_tools()` adapters and writes a `tool_calls` row with `idempotency_key`
- [x] Rolling `memory.summary` and `decisions` updates in `update_state`
- [x] **Sync 1:10:** run a mock job and show all candidates in `OUTREACH_DRAFTED`

## 1:10-1:40 Permission layer → checkpoint: approve → `CONTACTED` → `INTERESTED`
- [x] `permissions.py`: capability matrix, per-job policy, `auto_approve` / `auto_approve_actions`
- [x] `reject_candidate_proposal` is never auto-approved unless it is listed explicitly
- [x] Pending approval ends the run and emits `ApprovalRequested`
- [x] `ApprovalGranted` → `observe` loads the approval → jump to `execute` with the **stored payload**
- [x] `APPROVED` is a durable state between approval and send
- [x] Approval executes at most once (atomic `approved → executed`)
- [x] Defense in depth: restricted execution asserts `approval.status == approved`
- [x] After `send_outreach`: write the `messages` row and `contact_log`, emit `EmailSent`, pick a disposition (or the pinned `demo_disposition`), call `persona.reply`, insert a delayed `EmailReplied` (`not_before`). `no_reply` inserts nothing
- [x] `process_reply` classification → `INTERESTED` / `DECLINED` / `draft_reply`
- [x] **Test:** calling a restricted tool directly without an approval is refused
- [ ] **Sync 1:40**

## 1:40-2:50 Remaining lifecycle (C is building the UI meanwhile)
- [x] `Tick` every 15s per ACTIVE job: one follow-up after N hours, then `mark_stale` after M hours (shorten these in demo config), and job completion check
- [x] `schedule_interview` → persist `interviews` row → `INTERVIEWING`
- [x] `InterviewDone` → `process_interview` → `INTERVIEWED` → `evaluate_candidate` → `evaluations` row, `candidates.score`, `Evaluated`
- [x] `shortlist` if score ≥ threshold, else `reject_candidate_proposal`, `borderline` → `NEEDS_HUMAN`
- [x] Re-plan on rejection: redraft with feedback (max 3), interview rejected twice → escalate, rejection overridden → back to `EVALUATED`
- [x] `HumanResolved` → resume from `prev_state`; `STALE` reactivate → `CONTACTED`
- [ ] **Sync 2:50:** the voice interview path works end to end with B

## 2:50-3:40 Reliability + Langfuse → checkpoint: `NEEDS_HUMAN` with trace link
- [x] Retry taxonomy in `execute` (transient 1/2/4s, rate_limited with a 30s cap, not_found, invalid_input, fatal)
- [x] Retries exhausted → `ToolFailed` → `escalate` → `NEEDS_HUMAN`, `prev_state`, `trace_url`
- [x] `observability/langfuse.py`: one trace per graph run (`recruiter.run`, `session_id=job_id`, tags), node spans, tool spans, LLM generations
- [x] Store `agent_runs.langfuse_trace_id` and `trace_url`
- [x] Scores: `decision_valid`, `evaluation_score`
- [x] No Langfuse keys → no-op tracing, `trace_url` null, app still runs
- [x] Duplicate guard in `search_candidates`: `(job_id, ploid_id)` or `name_key` + shared URL → skip, add a memory note, no `CandidateFound`
- [x] `contact_log` check refuses a duplicate `send_outreach`
- [ ] **Test:** restart the server mid-run and confirm pending events resume with no state lost
- [ ] **Sync 3:40**

## 3:40-4:00 Demo prep
- [ ] Watch the activity log during rehearsal and fix any confusing reasoning text in the decide prompt
- [ ] Be on standby for agent bugs during the live demo
