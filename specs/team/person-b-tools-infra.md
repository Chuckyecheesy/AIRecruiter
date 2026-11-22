# Lane B: Tools & Infra

**You own:** `api/tools/`, `api/config.py`, `api/routes/webhooks.py`, `fixtures/`, `tests/tools/`, `Dockerfile`, `docker-compose.yml`, `.env.example`. You also own the ElevenLabs agent setup and the Maritime deploy.
**You don't touch:** `api/agent/`, `api/db/`, other `api/routes/`, `api/main.py`, `web/`.
**Read these specs:** [02 Tools & Adapters](../02-tools-and-adapters.md), [07 Deployment](../07-deployment-maritime.md), plus the ElevenLabs/webhook parts of [06](../06-api-and-dashboard.md)

**Key rule:** adapters are **pure**. Pydantic in, Pydantic out, or raise `ToolError`. **No DB access.** A persists all results.
**Others depend on you:** A needs `get_tools()` with working mocks by ~0:40. C needs `RubricGenerator` and `tools_status()` by ~1:00.

---

## 0:00-0:20 Kickoff (with everyone)
- [x] Add the tool I/O models from spec 02 (in `api/tools/models.py`; `contracts.py` re-exports them): `SearchCriteria`, `PloidCandidate`, `EnrichedProfile`, `ContactInfo`, `BrowserFindings`, `SendReceipt`, `InterviewSession`, `InterviewTranscript`, `Draft`, `Evaluation`, `Rubric`, `Disposition`
- [x] `config.py`: Pydantic settings with every env var from spec 07 (`TOOLS_MODE` and per-tool overrides, model IDs, `DEMO_INBOX`, `PERSONA_DELAY_S`, ...)
- [x] `.env.example` committed, `.env` git-ignored
- [ ] Merge to `main`

## 0:20-1:10 Mocks + Claude tools → checkpoint: core loop reaches `OUTREACH_DRAFTED` (with A)
- [x] `tools/base.py`: `ToolError(kind, retry_after_s)`, all `Protocol`s, `get_tools()` that picks mock/real per env var
- [x] `FAIL_TOOL=<name>` demo flag: the wrapper makes that tool raise `transient` every time (for the resilience demo)
- [x] `fixtures/ploid_candidates.json`: ~8 candidates, **including 1 duplicate and 1 thin profile** (completeness < 0.6)
- [x] `MockPloidClient.search` / `.enrich`
- [x] `fixtures/fiber_contacts.json` + `MockFiberClient` (one candidate raises `not_found`)
- [x] Demo-safety rewrite: every contact email becomes `DEMO_INBOX+{candidate_id}`
- [x] `Drafter.outreach(job, memory, feedback)` and `Drafter.reply(...)` (Claude). Feedback must make the redraft visibly different
- [x] `RubricGenerator.generate(form)`: 4-6 weighted criteria + interview themes
- [x] `tools_status()` returns `{tool: "mock"|"real"|"off"}` for C's `/health`
- [x] `tests/tools/`: each mock returns valid models
- [ ] **Merge by ~0:40** so A can swap their fakes for your mocks

## 1:10-1:40 Outreach tools → checkpoint: approve → `CONTACTED` → `INTERESTED`
- [x] `EmailSimulator.send` returns a fake `SendReceipt`. **No SMTP ever**
- [x] Hard guard: refuse a real send unless `ALLOW_REAL_EMAIL=true` **and** the recipient is on `EMAIL_ALLOWLIST`
- [x] `CandidatePersona.reply(profile, thread, disposition)` for `interested` / `not_interested` / `asks_question`
- [x] Helper `pick_disposition()` with 60/20/10/10 weights (A calls it when there is no pinned disposition)
- [ ] **Sync 1:40**

## 1:40-2:50 Voice + evaluation → checkpoint: real voice interview ends in `SHORTLISTED`
- [ ] Set up the ElevenLabs Conversational AI agent (voice, base prompt), record `ELEVENLABS_AGENT_ID`
- [x] `ElevenLabsClient.create_interview(candidate_id, questions, context)` returns a join URL (rubric questions via dynamic variables or per-session overrides)
- [x] `get_transcript(conversation_id)`
- [x] `verify_webhook(headers, body)` (signature check)
- [x] `routes/webhooks.py`: `POST /api/webhooks/elevenlabs` → verify → call A's `repo.emit_event(InterviewDone, transcript)`
- [x] Poller fallback (`ELEVENLABS_POLL=true`, every 10s) as a function A or C can start at startup
- [x] `MockElevenLabsClient`: scripted transcript after 15s (`fixtures/transcript.json`)
- [x] `Evaluator.evaluate(job, memory)` → `Evaluation` with per-criterion evidence quoted from memory
- [ ] Ask C to add your webhook router to `main.py`
- [ ] **Sync 2:50:** a team member does a live voice interview and it ends in `SHORTLISTED`

## 2:50-3:20 Real tools + Maritime → checkpoint: deployed
- [ ] Real `PloidClient` (if the API is confirmed). Fall back to the mock if it isn't working within 15 min
- [ ] Real `FiberClient` (same rule)
- [ ] `Dockerfile`: `python:3.12-slim`, build arg `WITH_BROWSER`, uvicorn on :8000, healthcheck `/health`, `/data` volume
- [ ] `docker-compose.yml` for local parity
- [ ] Deploy to Maritime: secrets, volume, `PUBLIC_BASE_URL`, ElevenLabs webhook URL (or `ELEVENLABS_POLL=true`)
- [ ] Confirm `GET /health` shows each tool's mode on Maritime
- [ ] Hand C the deployed API URL for `NEXT_PUBLIC_API_URL`
- [ ] **Decision at 3:30:** if the deploy isn't working, demo locally
- [ ] **Sync 3:20**

## Stretch (only if everything above is done)
- [ ] `BrowserTool` with Playwright: GET only, domain allowlist, 20s timeout, Claude summarizes into `BrowserFindings`. If it isn't working in 20 min, `BROWSER_MODE=off`
- [ ] `MaritimeRunner` (one VM per job)

## 3:40-4:00 Demo prep
- [ ] Warm the ElevenLabs session before the demo
- [ ] Check every API key is set on the demo environment
