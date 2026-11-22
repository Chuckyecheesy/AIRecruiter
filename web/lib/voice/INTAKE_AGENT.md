# CEO voice intake: ElevenLabs agent contract

The home page (`/`) lets the CEO **speak** a new role to an ElevenLabs Conversational AI agent. While they talk, the agent calls a **client tool** that fills in a role brief on screen. When the call ends, the brief becomes `brief-<role>.md` (downloadable) and is sent to `POST /api/jobs` as `freeText`, alongside the structured fields.

The UI is finished and runs a scripted **mock** call when no agent is configured. To go live, set up the agent below and set the env vars.

## 1. Env vars (`web/.env.local`)

```
# Option A: public agent (simplest; enable "public" / no auth in the agent's security settings)
NEXT_PUBLIC_ELEVENLABS_INTAKE_AGENT_ID=agent_xxx

# Option B: private agent. A server route that holds ELEVENLABS_API_KEY and returns
#   { "token": "<conversation token>" }        (WebRTC: GET /v1/convai/conversation/token?agent_id=...)
#   or { "signedUrl": "wss://..." }             (WebSocket: GET /v1/convai/conversation/get-signed-url?agent_id=...)
NEXT_PUBLIC_ELEVENLABS_TOKEN_URL=http://localhost:8000/api/voice/intake-token

# Force the scripted mock even when the vars above are set
# NEXT_PUBLIC_VOICE_MODE=mock
```

Use a **separate agent** from the candidate-interview agent. The prompts and tools are different.

## 2. Client tools (agent → Tools → Add tool → type **Client**)

Names must match exactly. Turn on **"Wait for response"** for `update_role_brief` so the agent hears what changed.

### `update_role_brief`
Description: *Record what the CEO has said about the role so far. Call it every time you learn something new; send only the fields that changed.*

| Parameter | Type | Description |
|---|---|---|
| `title` | string | Short job title, e.g. "AI Engineer (RAG + TypeScript)" |
| `must_haves` | string | Comma-separated required skills or experience to **add** |
| `nice_to_haves` | string | Comma-separated bonus skills to **add** |
| `location` | string | City / region / "remote" |
| `seniority` | string | One of: junior, mid, senior, staff |
| `notes` | string | Anything else the recruiter should know (culture, deal-breakers, comp, timeline). This is **appended**. |

All parameters are optional. Lists may also be sent as arrays. The UI merges them and removes duplicates.

### `finish_intake`
Description: *Call once the CEO confirms the brief is complete. Say a short goodbye first.* No parameters. The UI hangs up about 2.5s later.

## 3. Suggested system prompt

```
You are the recruiter at a fast-moving startup, taking a hiring brief from the CEO by voice.
Today is {{today}}.

Goal: in under 2 minutes, learn enough to source candidates:
role title, must-have skills, nice-to-haves, location, seniority, and anything that would rule someone in or out.

Rules:
- Ask ONE short question at a time. Sound like a sharp human recruiter, not a form.
- Every time the CEO gives you something new, immediately call update_role_brief with only the new fields.
- Don't read the brief back word for word. The CEO can see it filling in on screen.
- If an answer is vague ("good engineers"), ask one follow-up to make it concrete.
- When you have title + at least two must-haves + location + seniority, ask "Anything else before I start sourcing?"
- When they say no, say a short goodbye and call finish_intake.
```

First message: `Hi! I'm your recruiter. Who are we hiring?`

## 4. Where it lives in the UI code
- `lib/voice/elevenlabs.tsx`: the `useConversation` adapter (client tools, transcript, volume levels)
- `lib/voice/mock.ts`: the scripted fallback
- `lib/voice/brief.ts`: markdown generation
- `components/intake/VoiceIntake.tsx`: the page
