# 05 — Agent Graph (LangGraph)

One recruiter agent per job. The agent is a LangGraph `StateGraph` that processes **one event for one candidate** per invocation. The loop is created by events: each tool result emits an event that wakes the graph again.

## Graph state

```python
class AgentState(TypedDict):
    job_id: str
    candidate_id: str | None          # None for job-level events
    event: Event                      # the event that woke the agent
    job: Job
    candidate: Candidate | None
    memory: CandidateMemory | None
    valid_actions: list[ActionSpec]
    decision: Decision | None         # {action, args, reasoning}
    approval: Approval | None
    result: ToolResult | None
    error: ToolErrorInfo | None
    decide_attempts: int
    run_id: str
```

## Nodes

```
        ┌──────────┐   ┌────────┐   ┌──────────────────┐   ┌─────────┐   ┌──────────────┐
START ─►│ observe  │──►│ decide │──►│ permission_check │──►│ execute │──►│ update_state │──► END
        └──────────┘   └───┬────┘   └────────┬─────────┘   └────┬────┘   └──────┬───────┘
                           │ invalid action  │ pending           │ error         │ emits next event(s)
                           └─► retry/escalate└─► END (waits)     └─► retry/escalate
```

| Node | Responsibility |
|---|---|
| `observe` | Acquire the candidate lock, load job, candidate and memory, compute `valid_actions(state)`. For `ApprovalGranted`, load the approved `approval` and jump straight to `execute`. |
| `decide` | One Claude call (structured output) returns `{action, args, reasoning}`. Validates `action ∈ valid_actions`. Retry once on an invalid action, then `escalate`. |
| `permission_check` | See [03](03-permissions.md). READ/WRITE → proceed. RESTRICTED → create an approval (auto-approved, or pending). Pending ends the run. |
| `execute` | Dispatch to the adapter, with retries per the error taxonomy ([02](02-tools-and-adapters.md)). Writes `tool_calls`. Restricted actions assert an `approved` approval. |
| `update_state` | `apply_transition(state, action, result)`: update `candidates.state`, write memory (profile/contact/decisions/summary), write `messages`/`evaluations`/`interviews`, emit follow-up events, release the lock. |
| `escalate` (reached from decide/execute failures) | Set `NEEDS_HUMAN`, store `prev_state` and `trace_url`, emit `Escalated`. |

### Job-level events
- `JobCreated` → `observe` loads the job (no candidate). `valid_actions` = `[search_candidates]`. `execute` calls Ploid with the rubric-derived `SearchCriteria`, runs the duplicate guard, inserts `DISCOVERED` candidates (up to `max_candidates`), and emits one `CandidateFound` per candidate.
- `Tick` (emitted every 15s by the dispatcher for each ACTIVE job) → checks `CONTACTED` candidates for nudge/`STALE` rules, and checks whether the job should complete (shortlist target reached or all candidates terminal).

## Event dispatcher

A background asyncio task in the FastAPI process (one dispatcher per process, not per job):

```
loop every 1s:
  rows = SELECT events WHERE status='pending' AND (not_before IS NULL OR not_before <= now)
         ORDER BY id LIMIT 10
  for each row (grouped by candidate_id, serial within a candidate, concurrent across candidates):
      mark 'processing'
      graph.ainvoke(initial_state(event), config={"configurable": {"thread_id": thread_id}, "callbacks": [langfuse_handler]})
      mark 'done'  (or 'failed' + attempts++ on an unhandled exception; after 3 → escalate)
```
- **Per-candidate serialization:** the candidate lock plus ordered processing guarantees that two events for one candidate never run concurrently.
- **Concurrency across candidates:** `asyncio.Semaphore(MAX_CONCURRENT_RUNS=4)`.
- **Crash recovery:** on startup, events stuck in `processing` for more than 2 minutes are reset to `pending` (tools are idempotent via `idempotency_key`).
- **Pause:** job state `PAUSED` makes the dispatcher skip that job's events (they stay pending).

## Multiple recruiters
- One `jobs` row = one recruiter agent (`agent_thread_id`). The dispatcher interleaves events from all ACTIVE jobs, so adding a second role needs no code change.
- Different jobs can have different permission policies and rubrics.
- `AgentRunner` (see [07](07-deployment-maritime.md)) lets a job's agent run in its own Maritime VM later: same image, `JOB_ID` env var restricting the dispatcher to that job.

## Decide prompt contract

System prompt (short, stable):
- Role: "You are a recruiter agent working for the CEO on the role below. Choose the single best next action for this candidate."
- Rules: pick only from `valid_actions`, be concise in reasoning, never invent facts not in memory, prefer the cheapest action that makes progress.

User content:
```
JOB: {title}, must-haves, nice-to-haves, rubric
CANDIDATE: {name}, state={state}
MEMORY SUMMARY: {memory.summary}
RECENT DECISIONS: last 5
LATEST EVENT: {type, payload}   (e.g. EmailReplied with the text, or ApprovalRejected with the CEO feedback)
VALID ACTIONS:
  - enrich_candidate(): fetch full profile from Ploid
  - ...
```
Output schema:
```json
{"action": "browser_research", "args": {"urls": ["..."]}, "reasoning": "Profile completeness is 0.4; GitHub link available."}
```
The `args` schema is per action (Pydantic discriminated union). For most actions args are minimal and derived by code from memory (the LLM supplies only the judgment calls, e.g. which URLs to browse, or the tone of the draft).

`process_reply` is where the LLM reads the candidate's reply and classifies it `interested` / `declined` / `question`.

## Langfuse instrumentation
- One Langfuse **trace per graph invocation**: name `recruiter.run`, `session_id=job_id`, `user_id="ceo"`, tags `[job_id, candidate_id, state, event_type]`. Created through the LangChain/LangGraph callback handler.
- Spans: one per node, plus a span per tool call (`tool.ploid.enrich`, ...), plus generations for each LLM call (prompt, output, tokens).
- `agent_runs.langfuse_trace_id` and `trace_url` are stored so the dashboard can link to them.
- Scores: `decision_valid` (0/1) per decide call, and `evaluation_score` per candidate, attached to the trace.
- **Fallback behavior on failure:**
  1. Tool error: retry per taxonomy (in `execute`).
  2. Invalid decision: re-decide once with the validation error appended.
  3. Still failing, or an unhandled exception: `escalate` → candidate `NEEDS_HUMAN`, `trace_url` set, `Escalated` event, red badge in the dashboard with the trace link. The CEO clicks "Resume" (retry from `prev_state`) or "Skip".
- Env: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`. If keys are absent, tracing is a no-op and `trace_url` is null (the app must still run).

## Evaluation step
`evaluate_candidate` calls `Evaluator.evaluate(job, memory)` with the rubric, profile, research notes, the outreach thread and the interview transcript. It stores an `evaluations` row, sets `candidates.score`, emits `Evaluated`. The next decision is then `shortlist` (score ≥ `score_threshold`) or `reject_candidate_proposal` (below it). The `borderline` recommendation is routed to the CEO as `NEEDS_HUMAN` with the evaluation attached.

## Testing hooks
- `TOOLS_MODE=mock` runs the entire graph with no network except the Claude API.
- `LLM_MODE=stub` returns canned decisions for the happy path (CI and offline UI work).
