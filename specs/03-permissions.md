# 03 — Permission Layer

The recruiter can reason autonomously without being trusted to execute every action autonomously. The agent is not omnipotent: every action has a capability class, and the permission layer sits between `decide` and `execute`.

## Capability matrix

| Class | Actions | Behavior |
|---|---|---|
| **READ** | `search_candidates`, `enrich_candidate`, `find_contact`, `browser_research`, read candidate DB, fetch interview transcript | Always allowed |
| **WRITE** | update candidate state, write memory, `draft_outreach`, `redraft_outreach`, `draft_reply`, `evaluate_candidate`, `shortlist`, `mark_stale`, `escalate` | Always allowed; every write is audited in `events` |
| **RESTRICTED** | `send_outreach`, `send_followup`, `schedule_interview`, `reject_candidate_proposal` | Requires CEO approval unless the role's policy allows auto-approve |

`draft_*` and `shortlist` are WRITE, not RESTRICTED: they change only internal state, nothing leaves the system. Moving this boundary is a one-line config change.

## Policy config (per job)

```json
{
  "restricted_actions": ["send_outreach", "send_followup", "schedule_interview", "reject_candidate_proposal"],
  "auto_approve": false,
  "auto_approve_actions": [],
  "approval_timeout_minutes": null
}
```
- **Default:** all four restricted, `auto_approve=false`.
- **`auto_approve` toggle (demo speed):** when true, restricted actions in `auto_approve_actions` (or all restricted actions if the list is empty) are approved automatically by the system. The approval row is still created and marked `decided_by="auto"`, so the audit trail and the dashboard feed are unchanged.
- The toggle is exposed in the dashboard per job, and can be changed while the agent is running. It takes effect on the next permission check.
- `reject_candidate_proposal` is *never* auto-approved, even when `auto_approve=true`, unless explicitly listed in `auto_approve_actions`. (Rejection is irreversible and sensitive.)

## Flow

```
decide → {action, args}
   │
   ▼
permission_check(job.policy, action)
   ├── READ / WRITE ─────────────────────────────► execute
   └── RESTRICTED
         ├── auto_approve applies ──► create approval(status=approved, decided_by=auto) ─► execute
         └── else ──► create approval(status=pending) ─► emit ApprovalRequested
                         │          (graph run ends; candidate waits)
                         ▼
                  CEO decides in the dashboard
                   ├── approve (optional edits to the payload) ─► status=approved ─► emit ApprovalGranted ─► execute
                   └── reject (with feedback)                 ─► status=rejected ─► emit ApprovalRejected ─► agent re-plans
```

### Approval record
| Field | Notes |
|---|---|
| `id`, `job_id`, `candidate_id` | |
| `action` | restricted action name |
| `payload` | exact payload to execute (e.g. email subject/body, interview questions). **The executed payload is the approved payload.** |
| `status` | `pending` / `approved` / `rejected` / `expired` / `executed` |
| `reasoning` | the agent's reasoning, shown to the CEO |
| `decided_by` | `ceo` / `auto` |
| `feedback` | CEO's free-text on rejection, or edits on approval |
| `created_at`, `decided_at`, `executed_at` | |

### Rules
1. **Payload integrity:** `execute` runs only from an `approved` approval row and the stored payload. If the CEO edits the payload at approval time, the edited version is stored and executed.
2. **Re-plan on rejection:** `ApprovalRejected` wakes the agent with the CEO feedback in context. Expected behavior per action:
   - `send_outreach` rejected → `redraft_outreach` with the feedback (state stays `OUTREACH_DRAFTED`).
   - `schedule_interview` rejected → the agent waits and notes it in memory. After 2 rejections it escalates (`NEEDS_HUMAN`).
   - `reject_candidate_proposal` rejected → the candidate is kept: back to `EVALUATED` with a "CEO overrode" note, and the agent may `shortlist` or leave it to the CEO (`NEEDS_HUMAN`).
3. **Max 3 redrafts** per candidate, then escalate.
4. **No bypass:** all restricted tools check `approval.status == approved` internally too (defense in depth, so a bug in the graph can't send an unapproved email).
5. **Idempotent execution:** an approval executes at most once (`status` moves to `executed` atomically).
6. **Timeout:** optional; with no timeout the candidate simply waits. Not needed for the demo.

## Audit
Each permission decision writes an `events` row (`ApprovalRequested`/`Granted`/`Rejected`) and appears in the dashboard activity log: "Agent wants to send email → Permission Layer → Allowed? → Fiber/Email sim | CEO approval".

## Demo story for judges
"The recruiter reasons on its own, but it has no standing authority to contact anyone, schedule, or reject. Those are routed through the permission layer to the CEO. Flip auto-approve and the same agent runs hands-free, and the audit trail doesn't change."
