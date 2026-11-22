# 02 — Tools and Adapters

Every external service is a **typed adapter** behind a Python `Protocol`, with a `Mock` and a `Real` implementation chosen by env var (`TOOLS_MODE=mock|real`, overridable per tool: `PLOID_MODE`, `FIBER_MODE`, ...). The agent graph only depends on the protocols.

All adapter methods:
- are `async`
- return a Pydantic model or raise `ToolError`
- are wrapped by `execute` (see [05](05-agent-graph.md)) which records a `tool_calls` row and a Langfuse span

## Error taxonomy

```python
class ToolError(Exception):
    kind: Literal["transient", "rate_limited", "not_found", "invalid_input", "fatal"]
    retry_after_s: float | None
```

| kind | Handling |
|---|---|
| `transient` | Retry up to 3x with exponential backoff (1s, 2s, 4s) |
| `rate_limited` | Retry after `retry_after_s` (cap 30s), up to 3x |
| `not_found` | No retry. The result is recorded; the agent decides the next action from memory (e.g. skip contact lookup) |
| `invalid_input` | No retry. Counts as a decide-error. One re-decide, then escalate |
| `fatal` | No retry, escalate immediately |

After retries are exhausted → emit `ToolFailed` → `escalate` → candidate `NEEDS_HUMAN` with the Langfuse trace URL stored on the candidate.

## Tool catalog

| Tool | Action(s) | Permission | Mode default | Real endpoint |
|---|---|---|---|---|
| `PloidClient` | `search_candidates`, `enrich_candidate` | READ | real if keys, else mock | TBD |
| `FiberClient` | `find_contact` | READ | real if keys, else mock | TBD |
| `BrowserTool` | `browser_research` | READ | real (Playwright) if available, else disabled | n/a |
| `EmailSimulator` | `send_outreach`, `send_followup` | RESTRICTED | **always simulated** in the demo | n/a |
| `CandidatePersona` | produces `EmailReplied` | internal | LLM | n/a |
| `ElevenLabsClient` | `schedule_interview`, `process_interview` | RESTRICTED (create), READ (fetch transcript) | real | TBD (Conversational AI) |
| `Evaluator` | `evaluate_candidate` | WRITE | Claude | n/a |
| `Drafter` | `draft_outreach`, `redraft_outreach`, `draft_reply` | WRITE | Claude | n/a |

## Interfaces

### PloidClient (candidate discovery and enrichment)
```python
class PloidClient(Protocol):
    async def search(self, criteria: SearchCriteria, limit: int) -> list[PloidCandidate]: ...
    async def enrich(self, ploid_id: str) -> EnrichedProfile: ...

class SearchCriteria(BaseModel):
    title: str; must_have_skills: list[str]; nice_to_have_skills: list[str]
    location: str | None; seniority: str | None; free_text: str | None

class PloidCandidate(BaseModel):
    ploid_id: str; name: str; headline: str | None; location: str | None
    profile_urls: list[str] = []

class EnrichedProfile(BaseModel):
    ploid_id: str; name: str
    skills: list[str]; projects: list[Project]; education: list[str]
    experience: list[Experience]; links: dict[str, str]   # github, portfolio, linkedin
    raw: dict                                              # keep for memory
    completeness: float                                    # 0-1, drives browser_research decision
```
Mock: `fixtures/ploid_candidates.json` with ~8 candidates (including 1 duplicate and 1 thin profile, to exercise the guard and the browser path).

### FiberClient (contact lookup)
```python
class FiberClient(Protocol):
    async def find_contact(self, name: str, links: dict[str, str], company_hint: str | None) -> ContactInfo: ...

class ContactInfo(BaseModel):
    email: str | None; phone: str | None; confidence: float; source: str
```
Raises `not_found` when nothing is found.
**Demo safety:** in the demo, the adapter's output email is *overwritten* by the safety layer with the configured `DEMO_INBOX` address plus `+candidateid`, so nothing real can be targeted (see EmailSimulator).

### BrowserTool (READ-only fallback research)
```python
class BrowserTool(Protocol):
    async def research(self, urls: list[str], questions: list[str]) -> BrowserFindings: ...

class BrowserFindings(BaseModel):
    pages: list[PageSummary]     # url, title, extracted_text (truncated to 8k chars)
    extracted_skills: list[str]; notable_projects: list[str]
```
- Runs headless Playwright inside the Maritime container. No form submission and no logins. GET navigation only, with a domain allowlist (`github.com`, personal portfolios the profile links to, `linkedin.com` public pages, best effort).
- Claude summarizes pages into `BrowserFindings`. The result is merged into memory.
- Triggered when `EnrichedProfile.completeness < 0.6`. Skipped if the tool is disabled.

### EmailSimulator (outbound, simulated)
```python
class EmailSender(Protocol):
    async def send(self, to: str, subject: str, body: str, thread_id: str) -> SendReceipt: ...
```
- Simulated mode: writes a `messages` row (`direction="out"`, `channel="email"`), logs to `contact_log`, and returns a fake `message_id`. **No SMTP call is ever made in the demo.**
- A hard guard in the code refuses any real send unless `ALLOW_REAL_EMAIL=true` and the recipient is on `EMAIL_ALLOWLIST`. Default: both unset.
- After `send`, emits `EmailSent` and schedules a persona reply (below).

### CandidatePersona (LLM auto-plays the candidate)
```python
class CandidatePersona(Protocol):
    async def reply(self, profile: EnrichedProfile, thread: list[Message], disposition: Disposition) -> str: ...
```
- `Disposition` is chosen at the time of send: `interested` (default 60%), `not_interested` (20%), `asks_question` (10%), `no_reply` (10%). It can be pinned per candidate through `candidate.demo_disposition` so the demo script is deterministic.
- Reply delay: `PERSONA_DELAY_S` (default 10-20s demo, jittered). Implemented as a delayed `EmailReplied` event row (`events.not_before`).
- `no_reply` produces no event; the `Tick` path (nudge, then `STALE`) handles it.

### ElevenLabsClient (voice interview)
```python
class ElevenLabsClient(Protocol):
    async def create_interview(self, candidate_id: str, questions: list[str], context: str) -> InterviewSession: ...
    async def get_transcript(self, conversation_id: str) -> InterviewTranscript | None: ...
    def verify_webhook(self, headers: dict, body: bytes) -> bool: ...

class InterviewSession(BaseModel):
    conversation_id: str | None; join_url: str; expires_at: datetime
class InterviewTranscript(BaseModel):
    conversation_id: str; turns: list[Turn]; duration_s: int; ended_at: datetime
```
- `create_interview` configures a Conversational AI agent session with a system prompt built from the rubric and the generated questions (dynamic variables or per-session overrides; TBD against the ElevenLabs API). It returns a join link that a team member opens in a browser.
- Completion: `POST /webhooks/elevenlabs` (post-call webhook) → verify the signature → store the transcript → emit `InterviewDone`.
- Fallback: a background poller (`ELEVENLABS_POLL=true`) checks `get_transcript` for sessions in `INTERVIEWING` every 10s.
- Mock: returns a scripted transcript after 15s. For dev only, so the pipeline can be tested without a voice call.

### Drafter and Evaluator (Claude)
```python
class Drafter:
    async def outreach(self, job: Job, memory: CandidateMemory, feedback: str | None) -> Draft
    async def reply(self, job: Job, memory: CandidateMemory, thread: list[Message]) -> Draft

class Evaluator:
    async def evaluate(self, job: Job, memory: CandidateMemory) -> Evaluation

class Evaluation(BaseModel):
    score: int                        # 0-100
    criteria: list[CriterionScore]    # name, weight, score, evidence (quotes/facts from memory)
    recommendation: Literal["shortlist", "reject", "borderline"]
    summary: str
```
Rubric weights come from `job.rubric` (generated at job creation from the role form; the CEO can edit it before the agent starts).

## Role to rubric (job creation)
`RubricGenerator.generate(form) -> Rubric` (Claude). The rubric has 4-6 weighted criteria derived from must-haves and nice-to-haves, plus interview question themes. It is stored on the job and shown in the dashboard.

## Model usage

| Use | Model | Notes |
|---|---|---|
| decide | `claude-sonnet-5-5` | structured output, low temperature |
| draft / reply / persona | `claude-sonnet-5-5` | |
| evaluate | `claude-sonnet-5-5` | rubric-bound, structured output |
| rubric generation | `claude-sonnet-5-5` | once per job |
Model IDs live in config (`MODEL_DECIDE`, etc.), so a cheaper model (e.g. Haiku) can be swapped in for persona and draft calls if latency matters.
