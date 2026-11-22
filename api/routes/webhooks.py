"""ElevenLabs post-call webhook (lane B). Jayden: include with `app.include_router(webhooks.router)`.

POST /api/webhooks/elevenlabs  -> verify signature -> parse -> repo.emit_event(InterviewDone)
GET  /api/webhooks/mock-interview/{conversation_id}  -> placeholder page for the mock join link
"""
from __future__ import annotations

import html
import inspect
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from api.tools import get_tools
from api.tools.elevenlabs import parse_webhook

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


async def emit_interview_done(job_id: str, candidate_id: str, payload: dict) -> None:
    """Hands the transcript to lane A. Expected repo signature (confirm with William):
        repo.emit_event(type, *, job_id, candidate_id, payload)  (sync or async)
    """
    try:
        from api.db import repo  # lane A
    except ImportError as e:
        raise HTTPException(503, "event store not available yet (api.db.repo missing)") from e
    res = repo.emit_event("InterviewDone", job_id=job_id, candidate_id=candidate_id, payload=payload)
    if inspect.isawaitable(res):
        await res


@router.post("/elevenlabs")
async def elevenlabs_webhook(request: Request) -> dict:
    body = await request.body()  # raw bytes: the signature is over the exact body
    if not get_tools().elevenlabs.verify_webhook(dict(request.headers), body):
        raise HTTPException(401, "invalid ElevenLabs signature")
    result = parse_webhook(body)
    if result is None:
        return {"ok": True, "ignored": True}
    if not result.candidate_id or not result.job_id:
        # Not one of ours (e.g. someone tested the agent in the dashboard). 200 so ElevenLabs doesn't retry.
        log.warning("webhook for conversation %s has no candidate_id/job_id", result.transcript.conversation_id)
        return {"ok": True, "ignored": True, "reason": "missing candidate_id/job_id dynamic variables"}
    await emit_interview_done(
        result.job_id, result.candidate_id,
        {"conversation_id": result.transcript.conversation_id,
         "transcript": result.transcript.model_dump(mode="json"), "source": "webhook"},
    )
    return {"ok": True}


@router.get("/mock-interview/{conversation_id}", response_class=HTMLResponse)
async def mock_interview_page(conversation_id: str) -> str:
    return (
        "<!doctype html><meta charset=utf-8><title>Mock interview</title>"
        "<body style='font-family:system-ui;max-width:36rem;margin:4rem auto;padding:0 1rem'>"
        "<h1>Mock voice interview</h1>"
        f"<p>Conversation <code>{html.escape(conversation_id)}</code> is simulated (ELEVENLABS_MODE=mock).</p>"
        "<p>A scripted transcript will arrive automatically after MOCK_INTERVIEW_DELAY_S seconds "
        "(via the interview poller).</p></body>"
    )
