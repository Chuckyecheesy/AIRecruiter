# Lane C: API & Dashboard (assistant-ui)

**You own:** `api/main.py`, `api/routes/` (all except `webhooks.py`), `api/db/queries.py`, `web/`, `scripts/reset_demo.py`, `scripts/smoke.py`, `tests/api/`, `Makefile`, `README.md`. You also own the demo script and rehearsal.
**You don't touch:** `api/agent/`, `api/db/repo.py`, `api/db/schema.sql`, `api/tools/`, `config.py`, Docker files.
**Read these specs:** [06 API & Dashboard](../06-api-and-dashboard.md) (the REST API is unchanged, and the UI section is replaced by this file), [08 Build Plan (demo script)](../08-build-plan.md), and skim [04 Data Model](../04-data-model.md)

**Key rules:** for **writes**, call A's `repo.py` functions (never `UPDATE` tables yourself). For **reads**, write your own SQL in `db/queries.py`.
**You depend on:** A's repo signatures + schema (0:20), B's `RubricGenerator` and `tools_status()` (~1:00). Until then, work against data seeded by `reset_demo.py`.

---

## Frontend: assistant-ui

The CEO talks to the recruiter through an **[assistant-ui](https://www.assistant-ui.com) chat thread**. The agent's activity stream *is* the conversation, and every restricted action appears in the thread as an interactive **tool UI card** (human-in-the-loop approval).

**Stack:** Next.js (App Router) + Tailwind + shadcn/ui + `@assistant-ui/react` (scaffold with `npx assistant-ui init`). Check the current assistant-ui docs for exact API names; the ones below are what we expect to use.

**Runtime:** use the **external store runtime** (`useExternalStoreRuntime`). We are *not* using the LangGraph Cloud runtime, because our graph is event-driven and runs on the backend, not a chat graph.
- Messages are **derived from the `events` stream** (SSE `/jobs/{id}/stream`), mapped in `lib/runtime.ts`.
- Each agent decision or result becomes an assistant message with a **tool-call part** named after the action (`send_outreach`, `evaluate_candidate`, ...) and the payload passed as args.
- `onNew` (the CEO types in the composer) → `POST /api/jobs/{id}/chat` (see below).

### Job page layout (`/jobs/[id]`)
```
┌──────────────── header: job title · PolicyToggle (auto-approve) · Shortlist ──────────────┐
│  Pipeline (kanban, compact)  │          assistant-ui Thread                                │
│  Sourcing / Outreach /       │  🧠 Decided find_contact for Sarah: "profile complete…"    │
│  Interview / Decision /      │  ┌─ EmailDraftCard (send_outreach) ─────────────────────┐   │
│  Closed                      │  │ To: Sarah · editable subject/body · reasoning        │   │
│  (click card → drawer)       │  │ [Approve]  [Reject with feedback]                    │   │
│                              │  └──────────────────────────────────────────────────────┘   │
│                              │  📧 Sent (simulated) · 📞 InterviewCard · EvaluationCard   │
│                              │  [ composer: "shorter, mention the AI study assistant" ]   │
└──────────────────────────────┴──────────────────────────────────────────────────────────────┘
```

### Tool UIs (`makeAssistantToolUI`, one per action)
| Tool UI | Action / event | Renders | Buttons → API |
|---|---|---|---|
| `EmailDraftCard` | `send_outreach`, `send_followup` | editable subject/body, reasoning | Approve → `/approvals/{id}/approve {payload}` · Reject → `/reject {feedback}` |
| `InterviewCard` | `schedule_interview` | questions; after approval: join link + **Copy join link**, live status, then transcript | Approve / Reject |
| `RejectionCard` | `reject_candidate_proposal` | score, reason, evidence | Approve / Keep candidate (reject) |
| `DecisionNote` | any decision | one-line action + reasoning (collapsible) | none |
| `EvaluationCard` | `evaluate_candidate` / `Evaluated` | score, per-criterion evidence, recommendation | none |
| `NeedsHumanCard` | `Escalated` | error, **Open trace in Langfuse** | Resume / Skip |
| `DuplicateNote` | duplicate skipped | "already in pipeline" | none |

When auto-approve is on, approval cards render as already approved (with a `decided_by: auto` badge), so the thread reads the same either way.

### Composer → `POST /api/jobs/{id}/chat` (owned by C, `routes/chat.py`)
- **Must have:** if a pending approval is focused or replied to, the text becomes the rejection **feedback** for it (powers the redraft demo moment).
- **Should have:** a lightweight Claude call with a few tools that call A's repo functions: `pause`, `resume`, `approve/reject <candidate>`, `status` ("who's shortlisted?"), answered from `db/queries.py`. The reply streams back as an assistant message.
- It never runs agent actions itself. It only goes through the same approval and job-state functions the buttons use.

---

## 0:00-0:20 Kickoff (with everyone)
- [ ] Add the API response shapes from spec 06 (`Candidate`, `Approval`, `EventRow`, job detail with counts) to `api/contracts.py`
- [ ] Mirror the contract in `web/lib/types.ts`
- [ ] `api/main.py`: FastAPI app, CORS for the dashboard origin, include routers, call A's `start_dispatcher()` on startup
- [ ] Scaffold `web/`: Next.js + Tailwind, then `npx assistant-ui init` (adds shadcn + the `Thread` component), `NEXT_PUBLIC_API_URL`
- [ ] `Makefile`: `make dev` runs uvicorn + `next dev`
- [ ] Merge to `main`

## 0:20-1:10 API + seed data (no working agent needed)
- [ ] `scripts/reset_demo.py`: drop + recreate the DB, seed a demo job with ~5 candidates **spread across every state**, one pending approval of each type, plenty of events, pinned dispositions
- [ ] `db/queries.py`: list jobs with counts by state, job detail, pipeline candidates, candidate detail, approvals by status, events after id (joined with approval ids so the UI can link cards to approvals)
- [ ] `routes/jobs.py`: `POST /jobs` (calls B's `RubricGenerator`; stub the rubric until it lands), `PATCH`, `start`, `pause`, `resume`, `GET` list/detail
- [ ] `routes/candidates.py`: `GET` pipeline/detail, `resume`, `skip`, `PATCH demo_disposition`
- [ ] `routes/approvals.py`: queue, `approve {payload?}`, `reject {feedback}` (via `repo.decide_approval`)
- [ ] `routes/events.py`: **SSE** `/jobs/{id}/stream` with `Last-Event-ID`, plus `GET /events?after=`
- [ ] `routes/admin.py`: `/admin/reset` (guarded by `ADMIN_ENABLED`), `/health` (uses B's `tools_status()`)
- [ ] `routes/chat.py`: stub that turns text into feedback for the targeted pending approval
- [ ] `tests/api/`: smoke test each route against seeded data

## 1:10-1:40 Runtime wiring → checkpoint: approvals through the API
- [ ] Approve/reject from `curl` causes the agent to continue (verify with A)
- [ ] `web/lib/api.ts` (typed fetch client), `web/lib/sse.ts` (SSE with a 2s poll fallback)
- [ ] `web/lib/runtime.ts`: event → `ThreadMessage` mapper (assistant text + tool-call parts), `useExternalStoreRuntime` with `onNew` → `/chat`
- [ ] `<AssistantRuntimeProvider>` + `<Thread />` render the seeded job's history
- [ ] **Sync 1:40**

## 1:40-2:20 Thread UI v1 → checkpoint: the demo flow works visually
- [ ] **New role page** (`/`): form → rubric preview (editable weights) → policy with the auto-approve toggle, shortlist target, threshold → "Start recruiter" → `/jobs/[id]`
- [ ] Tool UIs: `EmailDraftCard`, `InterviewCard`, `RejectionCard` with working Approve / Reject-with-feedback
- [ ] `DecisionNote` + simple status lines for 📧 sent / reply received / 📞 interview done
- [ ] Composer feedback → rejection → the redraft appears as a new `EmailDraftCard`
- [ ] `PolicyToggle` in the header: auto-approve on/off live
- [ ] `Pipeline` kanban side panel (5 column groups) with ⏳ / 🔴 badges, live via the same SSE stream
- [ ] **Sync 2:20:** walk the full mock flow in the browser

## 2:20-3:20 Remaining cards, drawer, shortlist
- [ ] `EvaluationCard`, `NeedsHumanCard` (trace link, Resume / Skip), `DuplicateNote`
- [ ] `InterviewCard` live states: join link + Copy button → in progress → transcript
- [ ] `CandidateDrawer` (from a pipeline card): Memory / Timeline / Conversation / Interview / Evaluation tabs
- [ ] Filter the thread by candidate (click a pipeline card → show only that candidate's messages)
- [ ] Shortlist page (`/jobs/[id]/shortlist`): ranked, score, summary, per-criterion breakdown
- [ ] **Sync 2:50:** the voice interview shows up live in the thread
- [ ] Point the dashboard at the Maritime URL once B has it deployed

## 3:20-3:40 Polish + verification
- [ ] `routes/chat.py` should-have: Claude with `pause/resume/approve/reject/status` tools
- [ ] `scripts/smoke.py`: create a job, auto-approve on, wait for a `SHORTLISTED` candidate (mock mode)
- [ ] Verify acceptance criterion: a role created **in the dashboard** produces ≥3 candidates that progress with no manual API calls
- [ ] Stretch: create the role by chatting (rubric rendered as an editable `RubricCard` tool UI); shortlist "copy as markdown"
- [ ] `README.md`: local run instructions

## 3:40-4:00 Demo prep (you lead, everyone joins)
- [ ] `reset_demo.py` with pinned dispositions: `interested, interested, not_interested, no_reply, interested`
- [ ] Rehearse the 9-step demo script in spec 08 at least twice, timed (≈4 min). Step 4 now happens in the thread: reject by typing feedback in the composer
- [ ] Assign who talks / who drives / who does the voice interview
- [ ] Record a fallback screen recording of a full successful run
- [ ] Check the `FAIL_TOOL=fiber` resilience step works (with A + B)
