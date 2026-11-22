# 01 — State Machine

The LLM decides *what to do*. The state machine decides *what is allowed*. The decide node is only ever shown the actions valid for the candidate's current state, and any action outside that set is rejected before execution.

## Candidate lifecycle

```
DISCOVERED ─► RESEARCHED ─► CONTACT_FOUND ─► OUTREACH_DRAFTED ─► APPROVED ─► CONTACTED
                                                                                │
                              ┌─────────────────────────────────────────────────┤
                              ▼                       ▼                         ▼
                          DECLINED                  STALE                  INTERESTED
                                                                                │
                                                                                ▼
                                          INTERVIEWING ─► INTERVIEWED ─► EVALUATED ─► SHORTLISTED
                                                                                │
                                                                                └────► REJECTED

(any non-terminal state) ──tool failure after retries──► NEEDS_HUMAN
```

### Terminal states
`SHORTLISTED`, `REJECTED`, `DECLINED`. `STALE` is terminal unless the CEO re-activates it. `NEEDS_HUMAN` is paused and resumes at the state it left on CEO action.

## Transition table

Actions marked `*` are RESTRICTED (see [03](03-permissions.md)).

| State | Allowed actions | Event that triggers the decision | Result state |
|---|---|---|---|
| (job) | `search_candidates` | `JobCreated` | creates candidates in `DISCOVERED` |
| `DISCOVERED` | `enrich_candidate` | `CandidateFound` | `RESEARCHED` |
| `RESEARCHED` | `browser_research` (only if the profile is thin) | `CandidateEnriched` | `RESEARCHED` (memory enriched) |
| `RESEARCHED` | `find_contact` | `CandidateEnriched` | `CONTACT_FOUND` |
| `RESEARCHED` | `reject_candidate_proposal`\* (clearly unqualified) | `CandidateEnriched` | pending approval → `REJECTED` |
| `CONTACT_FOUND` | `draft_outreach` | `ContactFound` | `OUTREACH_DRAFTED` |
| `OUTREACH_DRAFTED` | `send_outreach`\* | `OutreachDrafted` | pending approval, then `APPROVED` → `CONTACTED` |
| `OUTREACH_DRAFTED` | `redraft_outreach` | `ApprovalRejected` (CEO rejected the draft, with feedback) | `OUTREACH_DRAFTED` (new draft) |
| `APPROVED` | `send_outreach` (executes the approved send) | `ApprovalGranted` | `CONTACTED` |
| `CONTACTED` | `wait` | `EmailSent` | `CONTACTED` |
| `CONTACTED` | `send_followup`\* (no reply after N hours, max 1 nudge) | `Tick` | `CONTACTED` |
| `CONTACTED` | `process_reply` | `EmailReplied` | `INTERESTED` / `DECLINED` / stays `CONTACTED` (candidate asked a question → `draft_reply`) |
| `CONTACTED` | `mark_stale` (nudge sent, still no reply after M hours) | `Tick` | `STALE` |
| `INTERESTED` | `schedule_interview`\* | `EmailReplied` | pending approval, then `INTERVIEWING` |
| `INTERVIEWING` | `wait` (session link live) | `InterviewScheduled` | `INTERVIEWING` |
| `INTERVIEWING` | `process_interview` | `InterviewDone` | `INTERVIEWED` |
| `INTERVIEWED` | `evaluate_candidate` | `InterviewDone` | `EVALUATED` |
| `EVALUATED` | `shortlist` (score ≥ threshold) | `Evaluated` | `SHORTLISTED` |
| `EVALUATED` | `reject_candidate_proposal`\* (score < threshold) | `Evaluated` | pending approval → `REJECTED` |
| any non-terminal | `escalate` | `ToolFailed` (retries exhausted) | `NEEDS_HUMAN` |
| `NEEDS_HUMAN` | `resume` (CEO action) | `HumanResolved` | previous state |
| `STALE` | `reactivate` (CEO action) | `HumanResolved` | `CONTACTED` |

Notes:
- `APPROVED` is a durable state, so a crash between approval and the send does not lose the approval.
- Each action has exactly one tool or internal handler and one permission class.
- `wait` is a no-op action: the agent records "waiting on X" and sleeps until the next event.

## Job-level state

| State | Meaning |
|---|---|
| `DRAFT` | Role form saved, rubric not yet generated |
| `ACTIVE` | Agent running; sourcing and processing candidates |
| `PAUSED` | CEO paused the agent (no new decisions) |
| `COMPLETE` | CEO closed the role, or a target shortlist size was reached |

Job-level parameters on the job record: `shortlist_target` (default 3), `score_threshold` (default 75), `max_candidates` (default 5 for the demo), `permission_policy` (see 03).

## How the LLM is constrained

1. `valid_actions(candidate.state, job)` returns the action list from the table above.
2. The decide prompt contains the candidate memory summary plus ONLY those actions, with a one-line description of each.
3. The LLM returns structured output: `{action, args, reasoning}`.
4. The graph validates `action ∈ valid_actions`. If not, retry once with the error. If it fails again, `escalate`.
5. The transition is applied by code (`apply_transition(state, action, result)`), never by the LLM.

The LLM choosing *which of several valid actions* is where agency lives, for example `browser_research` vs. going straight to `find_contact`, or `draft_reply` vs. `schedule_interview`.

## Idempotency and guards
- **Duplicate guard:** runs inside `search_candidates`, before a candidate is inserted as `DISCOVERED` (see [04](04-data-model.md)). Duplicates never enter the state machine. The agent also checks `contact_log` before any send ("did we already contact this person?").
- **One in-flight action per candidate:** a per-candidate lock (`candidates.locked_by`, `locked_at`) prevents two events from running concurrent decisions.
- **Tools are re-runnable:** each executed action has an `idempotency_key = candidate_id + action + attempt_group`, stored in `tool_calls`.
