# 07 — Deployment (Maritime)

**Maritime hosts the agent. LangGraph orchestrates the agent.** The Maritime VM/container is where the recruiter agent *lives* and has network egress to Ploid, Fiber, ElevenLabs, Claude, Langfuse and a headless browser. LangGraph (inside the Python service) decides what the agent does.

> Maritime's concrete interface (CLI, SDK, dashboard, how a container is deployed, public URL and webhook support) is **TBD** and must be confirmed at the hackathon. This spec isolates that risk behind two things: a plain Docker image, and an `AgentRunner` abstraction.

## Deployable units

| Unit | Runs on | Notes |
|---|---|---|
| `recruiter-api` (FastAPI + LangGraph + dispatcher + browser tool) | **Maritime** | The agent runtime. SQLite files on a mounted volume, if Maritime supports one |
| `web` (Next.js dashboard) | Maritime (second service) or Vercel or local | Needs `NEXT_PUBLIC_API_URL` |
| Langfuse | Langfuse Cloud (preferred) | No self-hosting during the hackathon |
| ElevenLabs | SaaS | Needs a reachable webhook URL, or the polling fallback |

## Docker image (`Dockerfile`)
- Base: `python:3.12-slim`, plus Playwright with Chromium (`playwright install --with-deps chromium`) if the browser tool is enabled. Use a build arg `WITH_BROWSER=true|false` to keep the image small when it is off.
- Entry: `uvicorn api.main:app --host 0.0.0.0 --port 8000`.
- Healthcheck: `GET /health`.
- Data dir: `/data` (SQLite DB and checkpoints). **Risk:** if Maritime volumes are ephemeral, state is lost on redeploy. Mitigation for the demo: avoid redeploying mid-demo, and keep `scripts/reset_demo.py` for a fast reseed.

## Environment variables

| Var | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude |
| `MODEL_DECIDE`, `MODEL_DRAFT`, `MODEL_EVAL` | default `claude-sonnet-5-5` |
| `TOOLS_MODE` | `mock` / `real` (per-tool overrides: `PLOID_MODE`, `FIBER_MODE`, `ELEVENLABS_MODE`, `BROWSER_MODE`) |
| `PLOID_API_KEY`, `PLOID_BASE_URL` | TBD |
| `FIBER_API_KEY`, `FIBER_BASE_URL` | TBD |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_AGENT_ID`, `ELEVENLABS_WEBHOOK_SECRET`, `ELEVENLABS_POLL` | voice interview |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | tracing |
| `DEMO_INBOX` | demo address that all simulated outreach is addressed to |
| `ALLOW_REAL_EMAIL`, `EMAIL_ALLOWLIST` | default unset, so no real send is possible |
| `PERSONA_DELAY_S` | reply delay in seconds (default 15) |
| `MAX_CONCURRENT_RUNS` | default 4 |
| `JOB_ID` | optional: restrict this process's dispatcher to one job (one-recruiter-per-VM mode) |
| `ADMIN_ENABLED` | enables `/admin/reset` |
| `DATA_DIR` | default `/data` |
| `PUBLIC_BASE_URL` | used to build the webhook and join links |

Secrets come from Maritime's secret store (TBD), or from `.env` locally. `.env` is git-ignored and `.env.example` is committed.

## AgentRunner abstraction

```python
class AgentRunner(Protocol):
    async def start(self, job_id: str) -> RunnerHandle: ...
    async def stop(self, job_id: str) -> None: ...
    async def status(self, job_id: str) -> RunnerStatus: ...
```

| Implementation | Behavior | When |
|---|---|---|
| `InProcessRunner` (**default, build first**) | All jobs share one process and one dispatcher. `start` just marks the job `ACTIVE` and emits `JobCreated` | Demo and local development |
| `MaritimeRunner` (**stretch**) | `start` asks Maritime to launch a dedicated VM from the same image with `JOB_ID=<id>`, and `stop` tears it down. Needs a shared DB (Postgres) or a per-VM DB | Showing "multiple recruiters, each on its own VM" |

Because the dispatcher already interleaves jobs, "multiple recruiters" works in the default runner too. The Maritime runner only changes isolation.

## Multiple recruiters (design)
- Each job has its own `agent_thread_id`, rubric, permission policy and candidate set.
- **Demo plan:** one role live. If time permits, create a second job (e.g. a different role) to show two recruiters working in parallel on the dashboard.
- **Per-VM mode (stretch):** `MaritimeRunner` + `JOB_ID` env + per-VM SQLite. The dashboard aggregates by calling each VM's API; this requires a registry table, so it is out of scope for the 4-hour build.

## Browser tool on Maritime
Playwright headless Chromium inside the container (`--no-sandbox`, a single context, a 20s page timeout, a domain allowlist). If Maritime provides its own browser primitive, wrap it in the same `BrowserTool` interface instead. If neither works within 20 minutes, set `BROWSER_MODE=off` (the agent skips `browser_research`).

## Webhook exposure
- ElevenLabs post-call webhook → `POST {PUBLIC_BASE_URL}/api/webhooks/elevenlabs`.
- If Maritime gives no public HTTPS URL, set `ELEVENLABS_POLL=true` (the poller fetches transcripts for `INTERVIEWING` candidates every 10s). Local development can use a tunnel instead.

## Local run
```
cp .env.example .env            # add ANTHROPIC_API_KEY at minimum
TOOLS_MODE=mock make dev        # api on :8000, web on :3000
python scripts/reset_demo.py    # seed a demo job
```
`make dev` runs uvicorn and `next dev`. `docker compose up` is provided for parity with the Maritime image.

## Deploy checklist (Maritime)
1. Build and push the image (or let Maritime build from the repo; TBD).
2. Set env vars and secrets, and mount a volume at `/data` if supported.
3. Confirm `GET /health` shows each tool's mode.
4. Set `PUBLIC_BASE_URL`; configure the ElevenLabs webhook (or enable polling).
5. Deploy the dashboard with `NEXT_PUBLIC_API_URL`.
6. Run the smoke test: `python scripts/smoke.py` (create a job, auto-approve on, expect a shortlisted candidate in mock mode).
