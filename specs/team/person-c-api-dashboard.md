# Lane C: API & Dashboard

**You own:** `api/main.py`, `api/routes/` (all except `webhooks.py`), `api/db/queries.py`, `web/`, `scripts/reset_demo.py`, `scripts/smoke.py`, `tests/api/`, `Makefile`, `README.md`. You also own the demo script and rehearsal.
**You don't touch:** `api/agent/`, `api/db/repo.py`, `api/db/schema.sql`, `api/tools/`, `config.py`, Docker files.
**Read these specs:** [06 API & Dashboard](../06-api-and-dashboard.md) (the REST API is unchanged, and the UI section is replaced by this file), [08 Build Plan (demo script)](../08-build-plan.md), and skim [04 Data Model](../04-data-model.md)

**Key rules:** for **writes**, call A's `repo.py` functions (never `UPDATE` tables yourself). For **reads**, write your own SQL in `db/queries.py`.
**You depend on:** A's repo signatures + schema (0:20), B's `RubricGenerator` and `tools_status()` (~1:00). Until then, work against data seeded by `reset_demo.py`.

---

## Frontend: candidate list + inline approvals

> **Change (dropped chat):** the original plan had an assistant-ui chat thread with a composer. Every composer command (`approve`, `pause`, `resume`, `status`, feedback) duplicated a button, so chat and `POST /jobs/{id}/chat` were removed. The job page is now built around the candidates.

**Stack:** Next.js (App Router) + Tailwind + Zustand store (`lib/store.ts`) fed by SSE (`/jobs/{id}/stream`, 2s poll fallback).

### Job page layout (`/jobs/[id]`)
```
┌ header: job title · ● Running [Pause]/[Resume] · N waiting · PolicyToggle · Shortlist ┐
│  CandidateList (main)                                     │  ActivityFeed (aside)       │
│  NEEDS YOU                                                │  ◆ Sarah: drafted outreach  │
│  Sarah Chen   Waiting for you            [Details]        │  ⏳ Sarah: outreach waiting │
│   ┌─ EmailDraftCard (inline) ─────────────────────────┐   │  ↩ You sent back Sarah's…   │
│   │ editable subject/body · reasoning                 │   │  📧 Sent to Tom (simulated) │
│   │ [Approve and send]  [Reject with feedback]        │   │  📊 Evaluated Ana: 82       │
│   └───────────────────────────────────────────────────┘   │                             │
│  OUTREACH · INTERVIEW · SCORED · CLOSED groups            │  (filtered when a row is    │
│  (row click → filter activity; Details → drawer)          │   selected)                 │
└───────────────────────────────────────────────────────────┴─────────────────────────────┘
```
On mobile the activity column stacks below the list.

- `components/CandidateList.tsx`: a **Needs you** group (pending approval or `NEEDS_HUMAN`) on top with the card inline, then the `PIPELINE_COLUMNS` groups. Other rows show the latest decision and its reasoning.
- `components/ActivityFeed.tsx`: a read-only log built by `lib/activity.ts` (`buildFeed`) from events. Approval, evaluation and escalation entries are one-line links that scroll to the candidate or open the drawer. CEO feedback on a rejected draft shows as a "You sent back…" line.

### Cards (`components/tool-uis.tsx`)
| Card | Action / event | Renders | Buttons → API |
|---|---|---|---|
| `EmailDraftCard` | `send_outreach`, `send_followup` | editable subject/body, reasoning | Approve → `/approvals/{id}/approve {payload}` · Reject → `/reject {feedback}` |
| `InterviewCard` | `schedule_interview` | questions; after approval: join link + **Copy join link**, live status, then transcript | Approve / Reject |
| `RejectionCard` | `reject_candidate_proposal` | score, reason, evidence | Approve / Keep candidate (reject) |
| `NeedsHumanCard` | `Escalated` | error, **Open trace in Langfuse** | Resume / Skip |
| `DecisionNote`, `StatusLine`, `DuplicateNote` | feed lines | one-line action + reasoning | none |

`ApprovalCard` picks the right card by action. When auto-approve is on, approvals are decided immediately and only appear in the activity feed.

---

## 0:00-0:20 Kickoff (with everyone)
- [x] Add the API response shapes from spec 06 (`Candidate`, `Approval`, `EventRow`, job detail with counts) to `api/contracts.py`
- [x] Mirror the contract in `web/lib/types.ts`
- [x] `api/main.py`: FastAPI app, CORS for the dashboard origin, include routers, call A's `start_dispatcher()` on startup
- [x] Scaffold `web/`: Next.js + Tailwind + shadcn, `NEXT_PUBLIC_API_URL`
- [x] `Makefile`: `make dev` runs uvicorn + `next dev`
- [ ] Merge to `main`

## 0:20-1:10 API + seed data (no working agent needed)
- [x] `scripts/reset_demo.py`: drop + recreate the DB, seed a demo job with ~5 candidates **spread across every state**, one pending approval of each type, plenty of events, pinned dispositions
- [x] `db/queries.py`: list jobs with counts by state, job detail, pipeline candidates, candidate detail, approvals by status, events after id (joined with approval ids so the UI can link cards to approvals)
- [x] `routes/jobs.py`: `POST /jobs` (calls B's `RubricGenerator`; stub the rubric until it lands), `PATCH`, `start`, `pause`, `resume`, `GET` list/detail
- [x] `routes/candidates.py`: `GET` pipeline/detail, `resume`, `skip`, `PATCH demo_disposition`
- [x] `routes/approvals.py`: queue, `approve {payload?}`, `reject {feedback}` (via `repo.decide_approval`)
- [x] `routes/events.py`: **SSE** `/jobs/{id}/stream` with `Last-Event-ID`, plus `GET /events?after=`
- [x] `routes/admin.py`: `/admin/reset` (guarded by `ADMIN_ENABLED`), `/health` (uses B's `tools_status()`)
- [x] ~~`routes/chat.py`~~ (dropped: chat removed, buttons cover every command)
- [x] `tests/api/`: smoke test each route against seeded data

## 1:10-1:40 Runtime wiring → checkpoint: approvals through the API
- [ ] Approve/reject from `curl` causes the agent to continue (verify with A)
- [x] `web/lib/api.ts` (typed fetch client), `web/lib/sse.ts` (SSE with a 2s poll fallback)
- [x] `web/lib/activity.ts`: event → activity feed items (was the assistant-ui runtime mapper)
- [x] Job page renders the seeded job (candidate list + activity feed)
- [ ] **Sync 1:40**

## 1:40-2:20 Thread UI v1 → checkpoint: the demo flow works visually
- [x] **New role page** (`/`): form → rubric preview (editable weights) → policy with the auto-approve toggle, shortlist target, threshold → "Start recruiter" → `/jobs/[id]`
- [x] Tool UIs: `EmailDraftCard`, `InterviewCard`, `RejectionCard` with working Approve / Reject-with-feedback
- [x] `DecisionNote` + simple status lines for 📧 sent / reply received / 📞 interview done
- [ ] Reject with feedback on the inline card → the redraft appears as a new `EmailDraftCard` on the same row
- [x] `PolicyToggle` in the header: auto-approve on/off live
- [x] `CandidateList` (Needs you + 5 groups) with ⏳ / 🔴 accents, live via the same SSE stream
- [ ] **Sync 2:20:** walk the full mock flow in the browser

## 2:20-3:20 Remaining cards, drawer, shortlist
- [x] `NeedsHumanCard` (trace link, Resume / Skip), `DuplicateNote`; evaluations show in the drawer
- [x] `InterviewCard` live states: join link + Copy button → in progress → transcript
- [x] `CandidateDrawer` (from a pipeline card): Memory / Timeline / Conversation / Interview / Evaluation tabs
- [x] Filter the activity feed by candidate (click a row → show only that candidate's activity)
- [x] Shortlist page (`/jobs/[id]/shortlist`): ranked, score, summary, per-criterion breakdown
- [ ] **Sync 2:50:** the voice interview shows up live on the candidate's card
- [ ] Point the dashboard at the Maritime URL once B has it deployed

## 3:20-3:40 Polish + verification
- [x] ~~`routes/chat.py` should-have: Claude with tools~~ (dropped with chat)
- [x] `scripts/smoke.py`: create a job, auto-approve on, wait for a `SHORTLISTED` candidate (mock mode). _Reaches `OUTREACH_DRAFTED` today; `SHORTLISTED` waits on lane A's approval/send/interview/evaluate slices._
- [x] Verify acceptance criterion: a role created **in the dashboard** produces ≥3 candidates that progress with no manual API calls. _`smoke.py` uses the dashboard's call sequence: 4 candidates go DISCOVERED → OUTREACH_DRAFTED on their own._
- [ ] Stretch: shortlist "copy as markdown"
- [x] `README.md`: local run instructions

## 3:40-4:00 Demo prep (you lead, everyone joins)
- [ ] `reset_demo.py` with pinned dispositions: `interested, interested, not_interested, no_reply, interested`
- [ ] Rehearse the 9-step demo script in spec 08 at least twice, timed (≈4 min). Step 4: click **Reject with feedback** on the inline draft card
- [ ] Assign who talks / who drives / who does the voice interview
- [ ] Record a fallback screen recording of a full successful run
- [ ] Check the `FAIL_TOOL=fiber` resilience step works (with A + B)
