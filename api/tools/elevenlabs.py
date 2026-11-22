"""ElevenLabs voice interview (spec 02). create = RESTRICTED, fetch transcript = READ.

How the real flow works (see specs/team/elevenlabs-setup.md for the dashboard setup):
1. `create_interview` makes NO API call. It builds a join link to the agent's public talk-to page,
   passing per-candidate dynamic variables (questions, context, candidate_id, job_id) as base64 JSON.
   `conversation_id` is unknown until someone actually opens the link, so it is None.
2. When the call ends, ElevenLabs POSTs a `post_call_transcription` webhook. `parse_webhook` turns it
   into (candidate_id, job_id, transcript) using the dynamic variables echoed back.
3. Fallback (`ELEVENLABS_POLL=true`): `find_conversation(candidate_id, since)` lists the agent's recent
   conversations and matches on the candidate_id dynamic variable; `poll_interviews` loops over it.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from ulid import ULID

from api.tools.base import ToolError
from api.tools.models import InterviewSession, InterviewTranscript, Turn
from api.tools.ploid import _raise_for_status

log = logging.getLogger(__name__)

TRANSCRIPT_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "transcript.json"
TALK_TO_URL = "https://elevenlabs.io/app/talk-to"
SIGNATURE_TOLERANCE_S = 30 * 60
SESSION_TTL = timedelta(hours=2)


def build_dynamic_variables(
    candidate_id: str, questions: list[str], context: str,
    job_id: str | None = None, candidate_name: str | None = None,
) -> dict[str, str]:
    """Variables referenced in the agent prompt as {{candidate_name}}, {{interview_questions}}, etc.

    Every key must exist (ElevenLabs errors on missing placeholders), so blanks are "".
    """
    return {
        "candidate_id": candidate_id,
        "job_id": job_id or "",
        "candidate_name": candidate_name or "there",
        "interview_questions": "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1)),
        "interview_context": context[:4000],
    }


def _to_turns(items: list[dict]) -> list[Turn]:
    return [
        Turn(role="agent" if t.get("role") == "agent" else "candidate",
             text=t.get("message") or "", t_s=t.get("time_in_call_secs"))
        for t in items or []
        if t.get("message")
    ]


def transcript_from_conversation(data: dict) -> InterviewTranscript:
    """Works for both the GET /conversations/{id} body and the webhook's `data` object."""
    meta = data.get("metadata") or {}
    start = meta.get("start_time_unix_secs")
    dur = int(meta.get("call_duration_secs") or 0)
    ended = (datetime.fromtimestamp(start + dur, timezone.utc) if start else datetime.now(timezone.utc))
    return InterviewTranscript(
        conversation_id=data["conversation_id"],
        turns=_to_turns(data.get("transcript") or []),
        duration_s=dur,
        ended_at=ended,
    )


def dynamic_variables_of(data: dict) -> dict:
    return ((data.get("conversation_initiation_client_data") or {}).get("dynamic_variables")) or {}


@dataclass
class WebhookResult:
    candidate_id: str | None
    job_id: str | None
    transcript: InterviewTranscript


def parse_webhook(body: bytes) -> WebhookResult | None:
    """Returns None for webhook types we ignore (e.g. post_call_audio)."""
    payload = json.loads(body)
    if payload.get("type") != "post_call_transcription":
        return None
    data = payload["data"]
    dv = dynamic_variables_of(data)
    return WebhookResult(
        candidate_id=dv.get("candidate_id") or None,
        job_id=dv.get("job_id") or None,
        transcript=transcript_from_conversation(data),
    )


def verify_signature(secret: str, headers: dict, body: bytes, now: float | None = None) -> bool:
    """`ElevenLabs-Signature: t=<unix>,v0=<hex hmac_sha256(secret, f"{t}.{body}")>`, 30-min tolerance."""
    header = next((v for k, v in headers.items() if k.lower() == "elevenlabs-signature"), None)
    if not header or not secret:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, sig = parts.get("t"), parts.get("v0")
    if not ts or not sig or not ts.isdigit():
        return False
    if abs((now or time.time()) - int(ts)) > SIGNATURE_TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


class RealElevenLabsClient:
    def __init__(self, api_key: str, agent_id: str, webhook_secret: str | None,
                 base_url: str = "https://api.elevenlabs.io"):
        self._agent_id = agent_id
        self._secret = webhook_secret or ""
        self._http = httpx.AsyncClient(base_url=base_url, headers={"xi-api-key": api_key}, timeout=20)
        self._conv_by_candidate: dict[str, str] = {}

    async def create_interview(self, candidate_id: str, questions: list[str], context: str,
                               *, job_id: str | None = None, candidate_name: str | None = None) -> InterviewSession:
        if not questions:
            raise ToolError("invalid_input", "at least one interview question is required")
        dv = build_dynamic_variables(candidate_id, questions, context, job_id, candidate_name)
        vars_b64 = base64.b64encode(json.dumps(dv).encode()).decode()
        return InterviewSession(
            conversation_id=None,
            join_url=f"{TALK_TO_URL}?{urlencode({'agent_id': self._agent_id, 'vars': vars_b64})}",
            expires_at=datetime.now(timezone.utc) + SESSION_TTL,
        )

    async def _get(self, path: str, **params) -> dict:
        try:
            r = await self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.TransportError as e:
            raise ToolError("transient", f"elevenlabs network error: {e}") from e
        _raise_for_status(r, "elevenlabs")
        return r.json()

    async def get_transcript(self, conversation_id: str) -> InterviewTranscript | None:
        data = await self._get(f"/v1/convai/conversations/{conversation_id}")
        status = data.get("status")
        if status == "failed":
            raise ToolError("fatal", f"elevenlabs conversation {conversation_id} failed")
        if status != "done":
            return None  # initiated / in-progress / processing
        return transcript_from_conversation(data)

    async def find_conversation(self, candidate_id: str, since: datetime) -> str | None:
        """Polling fallback: which conversation belongs to this candidate? None if not started yet."""
        if candidate_id in self._conv_by_candidate:
            return self._conv_by_candidate[candidate_id]
        listing = await self._get("/v1/convai/conversations", agent_id=self._agent_id, page_size=30,
                                  call_start_after_unix=int(since.timestamp()) - 60)
        for conv in listing.get("conversations", []):
            detail = await self._get(f"/v1/convai/conversations/{conv['conversation_id']}")
            cid = dynamic_variables_of(detail).get("candidate_id")
            if cid:
                self._conv_by_candidate.setdefault(cid, conv["conversation_id"])
        return self._conv_by_candidate.get(candidate_id)

    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        return verify_signature(self._secret, headers, body)


class MockElevenLabsClient:
    """Dev only: the transcript becomes available `delay_s` after create_interview."""

    def __init__(self, public_base_url: str, delay_s: float = 15.0, fixture: Path = TRANSCRIPT_FIXTURE):
        self._base = public_base_url.rstrip("/")
        self._delay = delay_s
        self._fixture = json.loads(fixture.read_text())
        self._created: dict[str, float] = {}
        self._conv_by_candidate: dict[str, str] = {}

    async def create_interview(self, candidate_id: str, questions: list[str], context: str,
                               *, job_id: str | None = None, candidate_name: str | None = None) -> InterviewSession:
        if not questions:
            raise ToolError("invalid_input", "at least one interview question is required")
        conv_id = f"mock_conv_{ULID()}"
        self._created[conv_id] = time.monotonic()
        self._conv_by_candidate[candidate_id] = conv_id
        return InterviewSession(
            conversation_id=conv_id,
            join_url=f"{self._base}/api/webhooks/mock-interview/{conv_id}",
            expires_at=datetime.now(timezone.utc) + SESSION_TTL,
        )

    async def get_transcript(self, conversation_id: str) -> InterviewTranscript | None:
        started = self._created.get(conversation_id)
        if started is None:
            # Unknown id (e.g. after a restart): treat as done so the demo never hangs.
            started = time.monotonic() - self._delay
        if time.monotonic() - started < self._delay:
            return None
        return InterviewTranscript(
            conversation_id=conversation_id,
            turns=[Turn(**t) for t in self._fixture["turns"]],
            duration_s=self._fixture["duration_s"],
            ended_at=datetime.now(timezone.utc),
        )

    async def find_conversation(self, candidate_id: str, since: datetime) -> str | None:
        return self._conv_by_candidate.get(candidate_id)

    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        return True


# ---------- Polling fallback loop (no DB access: lane A supplies the callbacks) ----------

@dataclass
class PendingInterview:
    candidate_id: str
    conversation_id: str | None
    created_at: datetime


GetPending = Callable[[], Awaitable[list[PendingInterview]] | list[PendingInterview]]
OnDone = Callable[[str, InterviewTranscript], Awaitable[None] | None]


async def _maybe_await(x):
    return await x if inspect.isawaitable(x) else x


async def poll_interviews(client, get_pending: GetPending, on_done: OnDone,
                          interval_s: float = 10.0, stop: asyncio.Event | None = None) -> None:
    """Every `interval_s`: for each INTERVIEWING candidate, find its conversation and, when done,
    call `on_done(candidate_id, transcript)` (lane A emits InterviewDone there).

    Start it at app startup when ELEVENLABS_POLL=true:
        asyncio.create_task(poll_interviews(get_tools().elevenlabs, repo.pending_interviews, on_done))
    """
    seen: set[str] = set()
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            for p in await _maybe_await(get_pending()):
                if p.candidate_id in seen:
                    continue
                conv = p.conversation_id or await client.find_conversation(p.candidate_id, p.created_at)
                if not conv:
                    continue
                transcript = await client.get_transcript(conv)
                if transcript is not None:
                    seen.add(p.candidate_id)
                    await _maybe_await(on_done(p.candidate_id, transcript))
        except ToolError as e:
            log.warning("interview poll error: %r", e)
        except Exception:  # never let the poller die mid-demo
            log.exception("interview poll crashed; continuing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
