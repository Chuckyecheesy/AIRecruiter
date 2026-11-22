# ElevenLabs setup (Lane B)

Code: [api/tools/elevenlabs.py](../../api/tools/elevenlabs.py), [api/routes/webhooks.py](../../api/routes/webhooks.py).
Menu names below may differ slightly in the current ElevenLabs dashboard.

## How it works
1. `schedule_interview` is approved → lane A calls
   `tools.elevenlabs.create_interview(candidate_id, questions, context, job_id=..., candidate_name=...)`.
2. That returns a **join link** to the agent's public talk-to page, with the questions and IDs as
   dynamic variables: `https://elevenlabs.io/app/talk-to?agent_id=…&vars=<base64 JSON>`.
   No API call, and `conversation_id` is `None` until someone opens the link.
3. A teammate opens the link and plays the candidate.
4. After the call, ElevenLabs POSTs to `{PUBLIC_BASE_URL}/api/webhooks/elevenlabs`. We check the
   `ElevenLabs-Signature` HMAC, read `candidate_id` / `job_id` back from the dynamic variables, and
   emit `InterviewDone` with the transcript.
5. With no public URL, set `ELEVENLABS_POLL=true`: `poll_interviews()` finds the conversation by its
   `candidate_id` variable every 10s.

## Dashboard checklist
- [ ] Create a Conversational AI agent ("Recruiter Screen"), choose a voice, and set the LLM.
- [ ] Paste the **system prompt** and **first message** below.
- [ ] Add placeholder values for each dynamic variable (`candidate_id`, `job_id`, `candidate_name`,
      `interview_questions`, `interview_context`) so the dashboard "Test agent" button works.
- [ ] Make the agent **public** (no authentication), otherwise the talk-to link won't open.
- [ ] Set the max call duration to about 6 minutes so a demo call can't run long.
- [ ] Copy the agent ID into `ELEVENLABS_AGENT_ID` and the API key into `ELEVENLABS_API_KEY`.
- [ ] Workspace settings → webhooks: add a post-call webhook to
      `{PUBLIC_BASE_URL}/api/webhooks/elevenlabs` with transcription events enabled. Copy the HMAC
      secret into `ELEVENLABS_WEBHOOK_SECRET`, then attach the webhook to the agent.
- [ ] Set `ELEVENLABS_MODE=real`, then run one test call end to end.
- [ ] No public URL (local, or Maritime without HTTPS)? Set `ELEVENLABS_POLL=true` instead.

## System prompt
```
You are a friendly, concise technical recruiter running a short voice screening call
for a startup, on behalf of the CEO.

Candidate: {{candidate_name}}
What we know about the role and candidate:
{{interview_context}}

Ask these questions in order, one at a time:
{{interview_questions}}

Rules:
- Keep the whole call under 5 minutes. One short follow-up per question at most, only if the answer was vague.
- Don't evaluate, score, or give feedback on answers. Never promise next steps or an offer.
- If the candidate asks about the role, answer briefly from the context above; if unknown, say the CEO will follow up.
- After the last question, thank them, say the team will be in touch, and end the call.
```

## First message
```
Hi {{candidate_name}}, thanks for making time! This is a quick five-minute screen for the role. Ready to start?
```

## Env vars
| Var | Value |
|---|---|
| `ELEVENLABS_MODE` | `real` (default follows `TOOLS_MODE`; mock = scripted transcript after `MOCK_INTERVIEW_DELAY_S`) |
| `ELEVENLABS_API_KEY` | from the profile → API keys page |
| `ELEVENLABS_AGENT_ID` | `agent_…` |
| `ELEVENLABS_WEBHOOK_SECRET` | from the webhook settings |
| `ELEVENLABS_POLL` | `true` if the webhook can't reach us |

## What lane A needs to do (tell William)
- Call `create_interview(..., job_id=job.id, candidate_name=name)`, so the webhook can route the transcript.
- Store `join_url` and `created_at` in `interviews`. `conversation_id` may be `None`.
- Handle `InterviewDone` with payload `{conversation_id, transcript, source}`, and ignore a duplicate
  if the candidate is no longer `INTERVIEWING` (both the webhook and the poller could fire).
- If `ELEVENLABS_POLL=true`, or in mock mode, start the poller at app startup:
  `poll_interviews(tools.elevenlabs, get_pending, on_done)`. Here `get_pending` returns
  `PendingInterview(candidate_id, conversation_id, created_at)` for INTERVIEWING candidates, and
  `on_done(candidate_id, transcript)` emits `InterviewDone`.
- `repo.emit_event("InterviewDone", job_id=..., candidate_id=..., payload=...)` is the signature the
  webhook route calls. Tell me if yours differs.
