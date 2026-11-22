# 06 — API and Dashboard

Backend: FastAPI (Python). Frontend: Next.js (App Router) + Tailwind. Single user (the CEO), no auth for the demo. An optional shared token header `X-Demo-Token` can protect the deployed instance.

## REST API

Base path `/api`. JSON in/out.

### Jobs
| Method | Path | Purpose |
|---|---|---|
| POST | `/jobs` | Create a job from the role form. Generates the rubric (Claude); returns the job in `DRAFT` with the rubric for review |
| PATCH | `/jobs/{id}` | Edit the rubric, thresholds, permission policy (incl. `auto_approve`) |
| POST | `/jobs/{id}/start` | `DRAFT` → `ACTIVE`; emits `JobCreated` |
| POST | `/jobs/{id}/pause` · `/resume` | Pause or resume the agent |
| GET | `/jobs` · `/jobs/{id}` | List or detail, including counts by state |

### Candidates
| Method | Path | Purpose |
|---|---|---|
| GET | `/jobs/{id}/candidates` | Pipeline view: candidates with state, score, flags |
| GET | `/candidates/{id}` | Detail: memory, messages, interview, evaluation, decisions, events, `trace_url` |
| POST | `/candidates/{id}/resume` | `NEEDS_HUMAN` → `prev_state`; emits `HumanResolved` |
| POST | `/candidates/{id}/skip` | Move to `REJECTED`/terminal without agent action (CEO override, logged) |
| PATCH | `/candidates/{id}` | Set `demo_disposition` (demo control) |

### Approvals
| Method | Path | Purpose |
|---|---|---|
| GET | `/jobs/{id}/approvals?status=pending` | Approval queue |
| POST | `/approvals/{id}/approve` | Body `{payload?: edited}` → emits `ApprovalGranted` |
| POST | `/approvals/{id}/reject` | Body `{feedback}` → emits `ApprovalRejected` |

### Events and live updates
| Method | Path | Purpose |
|---|---|---|
| GET | `/jobs/{id}/stream` | **SSE** stream of events as they are written (the dashboard updates live). Supports `Last-Event-ID` for resume |
| GET | `/jobs/{id}/events?after={id}` | Poll fallback and activity log |

### Webhooks and demo controls
| Method | Path | Purpose |
|---|---|---|
| POST | `/webhooks/elevenlabs` | Post-call webhook: verify signature, store transcript, emit `InterviewDone` |
| POST | `/candidates/{id}/simulate/reply` | (Optional) force a persona disposition now |
| POST | `/admin/reset` | Reset the demo DB (guarded by the `ADMIN_ENABLED` env var) |
| GET | `/health` | Liveness, plus the status of each tool (mock/real) |

## Key response shapes

```ts
type Candidate = {
  id: string; name: string; state: CandidateState; score: number | null;
  headline: string | null; needsHuman: boolean; traceUrl: string | null;
  pendingApprovalId: string | null; updatedAt: string;
}
type Approval = {
  id: string; candidateId: string; candidateName: string;
  action: 'send_outreach' | 'send_followup' | 'schedule_interview' | 'reject_candidate_proposal';
  payload: Record<string, unknown>;     // email draft, interview questions, rejection reason
  reasoning: string; status: string; createdAt: string;
}
type EventRow = { id: number; type: string; candidateId: string | null; payload: any; createdAt: string; }
```

## Dashboard screens

### 1. New role (`/`)
Form: title, must-have skills, nice-to-haves, location, seniority, free text. Submit → rubric preview (editable weights and criteria), permission policy (four restricted actions with the `auto-approve` toggle), shortlist target, score threshold. "Start recruiter" → job `ACTIVE`.

### 2. Job page (`/jobs/[id]`), the main demo screen
Three regions:
- **Pipeline (kanban):** columns by state grouped as `Sourcing` (DISCOVERED, RESEARCHED, CONTACT_FOUND), `Outreach` (OUTREACH_DRAFTED, APPROVED, CONTACTED), `Interview` (INTERESTED, INTERVIEWING, INTERVIEWED), `Decision` (EVALUATED, SHORTLISTED), `Closed` (REJECTED, DECLINED, STALE). Cards show name, headline, score and badges (⏳ waiting approval, 🔴 NEEDS_HUMAN). Cards animate when state changes (SSE).
- **Approval queue (right rail, top):** each item shows the action, the candidate, the agent's reasoning, and the exact payload (editable email body). Buttons: **Approve** · **Reject with feedback**. A header toggle shows auto-approve state.
- **Agent activity log (right rail, bottom):** live feed such as "🧠 Decided `find_contact` (reasoning…)", "🔒 Permission: send_outreach → CEO approval", "✅ Approved", "📧 Sent (simulated)", "📞 Interview done". Entries link to the candidate; failure entries link to the Langfuse trace.

### 3. Candidate drawer (`/jobs/[id]?candidate=…`)
Tabs: **Memory** (profile, research notes, contact, summary), **Timeline** (events and decisions with reasoning), **Conversation** (email thread, simulated badge), **Interview** (join link while live, then transcript), **Evaluation** (score, per-criterion evidence, recommendation). If `NEEDS_HUMAN`: banner with the error, **Open trace in Langfuse**, and **Resume** / **Skip** buttons. The interview tab includes a **Copy join link** button for the team member playing the candidate.

### 4. Shortlist view (`/jobs/[id]/shortlist`)
Ranked candidates with score, summary and a per-criterion breakdown. One-click "export" (copy as markdown).

## Realtime
- The SSE endpoint streams `events` rows. The UI reducer applies them to the local pipeline state, then refetches the affected candidate for details.
- Fallback: poll `/events?after=` every 2s if SSE fails.

## Frontend structure
```
web/
  app/page.tsx                       # new role
  app/jobs/[id]/page.tsx             # pipeline + approvals + log
  app/jobs/[id]/shortlist/page.tsx
  components/{Pipeline,CandidateCard,ApprovalQueue,ActivityLog,CandidateDrawer,PolicyToggle}.tsx
  lib/api.ts, lib/sse.ts, lib/types.ts
```
Config: `NEXT_PUBLIC_API_URL`. CORS is enabled on the API for the dashboard origin.

## Backend structure
```
api/
  main.py            # FastAPI app, startup: init DB, start dispatcher
  routes/{jobs,candidates,approvals,events,webhooks,admin}.py
  agent/{graph,nodes,state_machine,permissions,dispatcher,prompts}.py
  tools/{base,ploid,fiber,browser,email_sim,elevenlabs,persona,drafter,evaluator}.py
  db/{schema.sql,repo.py}
  observability/langfuse.py
  config.py
```
