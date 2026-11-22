"""ElevenLabs post-call webhook (lane B). Jayden: include with `app.include_router(webhooks.router)`.

POST /api/webhooks/elevenlabs  -> size cap -> verify signature -> parse -> verify interview token
                                  -> dedupe on conversation_id -> repo.emit_event(InterviewDone)
GET  /api/webhooks/mock-interview/{conversation_id}  -> placeholder page for the mock join link

If lane C adds an X-Demo-Token check, this route must be exempt (ElevenLabs can't send that header);
it is protected by the HMAC signature + per-interview token instead.
"""
from __future__ import annotations

import html
import inspect
import logging
from collections import OrderedDict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from api.config import get_settings
from api.tools import get_tools
from api.tools.elevenlabs import parse_webhook

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

_SEEN_MAX = 1000
_seen_conversations: OrderedDict[str, None] = OrderedDict()  # in-process dedupe (retries / replays)


def _seen(conv_id: str) -> bool:
    return conv_id in _seen_conversations


def _mark_seen(conv_id: str) -> None:
    _seen_conversations[conv_id] = None
    while len(_seen_conversations) > _SEEN_MAX:
        _seen_conversations.popitem(last=False)


async def emit_interview_done(job_id: str, candidate_id: str, payload: dict) -> None:
    """Hands the transcript to lane A: repo.emit_event(job_id, type, candidate_id, payload) (sync).
    Called with keywords only, so argument order can't silently break.
    Lane A should also ignore InterviewDone if the candidate is no longer INTERVIEWING
    (dedupe here is per-process only).
    """
    try:
        from api.db import repo  # lane A
    except ImportError as e:
        raise HTTPException(503, "event store not available yet (api.db.repo missing)") from e
    res = repo.emit_event(job_id=job_id, type="InterviewDone", candidate_id=candidate_id, payload=payload)
    if inspect.isawaitable(res):
        await res


async def _read_capped(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "payload too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(413, "payload too large")
    return bytes(body)


@router.post("/elevenlabs")
async def elevenlabs_webhook(request: Request) -> dict:
    body = await _read_capped(request, get_settings().webhook_max_bytes)  # raw bytes: signature is over them
    client = get_tools().elevenlabs
    if not client.verify_webhook(dict(request.headers), body):
        raise HTTPException(401, "invalid ElevenLabs signature")
    try:
        result = parse_webhook(body)
    except ValueError as e:
        raise HTTPException(400, "malformed webhook payload") from e
    if result is None:
        return {"ok": True, "ignored": True}

    conv_id = result.transcript.conversation_id
    ident = client.identify(result)
    if ident is None:
        # Not one of our links, or the vars were tampered with / the link expired.
        # 200 so ElevenLabs doesn't retry.
        log.warning("webhook %s: no valid interview token (claimed candidate %r)", conv_id,
                    result.claimed_candidate_id)
        return {"ok": True, "ignored": True, "reason": "missing or invalid interview token"}
    if _seen(conv_id):
        return {"ok": True, "duplicate": True}

    candidate_id, job_id = ident
    await emit_interview_done(job_id, candidate_id, {
        "conversation_id": conv_id,
        "transcript": result.transcript.model_dump(mode="json"),
        "source": "webhook",
    })
    _mark_seen(conv_id)
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
