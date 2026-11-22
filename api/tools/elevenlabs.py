"""ElevenLabs voice interview (spec 02). create = RESTRICTED, fetch transcript = READ.

How the real flow works (see specs/team/elevenlabs-setup.md for the dashboard setup):
1. `create_interview` makes NO API call. It builds a join link to the agent's public talk-to page,
   passing per-candidate dynamic variables as base64 JSON. `conversation_id` is unknown until someone
   opens the link, so it is None.
2. The link is PUBLIC and its variables are client-controlled, so they carry an HMAC token over
   (candidate_id, job_id, expiry). A webhook or poll match is only trusted if the token verifies and
   the call started before expiry. Never put rubric/scoring details in `context`: it is visible.
3. When the call ends, ElevenLabs POSTs a signed `post_call_transcription` webhook. The route checks
   the signature, parses it, then `client.identify(result)` checks the token.
4. Fallback (`ELEVENLABS_POLL=true`): `find_conversation` lists the agent's recent conversations and
   matches finished ones on a valid token; `poll_interviews` loops over it.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import inspect
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from ulid import ULID

from api.tools.base import ToolError
from api.tools.http import json_body, path_id, raise_for_status
from api.tools.models import InterviewSession, InterviewTranscript, Turn

log = logging.getLogger(__name__)

TRANSCRIPT_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "transcript.json"
TALK_TO_URL = "https://elevenlabs.io/app/talk-to"
SIGNATURE_TOLERANCE_S = 30 * 60
MAX_CONTEXT_CHARS = 1500


# ---------- per-interview token (the join link is public and editable) ----------

class InterviewSigner:
    def __init__(self, secret: str, ttl: timedelta = timedelta(hours=2)):
        if not secret:
            raise ValueError("InterviewSigner needs a non-empty secret")
        self._key = secret.encode()
        self.ttl = ttl

    def _mac(self, candidate_id: str, job_id: str, exp: int) -> str:
        msg = f"{candidate_id}|{job_id}|{exp}".encode()
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()[:32]

    def sign(self, candidate_id: str, job_id: str, now: float | None = None) -> tuple[int, str]:
        exp = int((now or time.time()) + self.ttl.total_seconds())
        return exp, self._mac(candidate_id, job_id, exp)

    def identify(self, dv: dict, started_at_unix: float | None) -> tuple[str, str] | None:
        """(candidate_id, job_id) if the variables carry a valid, unexpired token, else None."""
        cid, jid = str(dv.get("candidate_id") or ""), str(dv.get("job_id") or "")
        token, exp_s = str(dv.get("interview_token") or ""), str(dv.get("interview_expires") or "")
        if not (cid and jid and token and exp_s.isdigit()):
            return None
        if not hmac.compare_digest(self._mac(cid, jid, int(exp_s)).encode(), token.encode()):
            return None
        if started_at_unix is not None and started_at_unix > int(exp_s):
            return None  # link reused after it expired
        return cid, jid


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
        "interview_context": context[:MAX_CONTEXT_CHARS],
    }


# ---------- parsing ----------

# Expressive-voice tags the agent emits, e.g. "[friendly]", "[warm]" (not spoken content).
_VOICE_TAG = re.compile(r"\[[a-z][a-z ]{0,20}\]\s*")


def _to_turns(items: list[dict]) -> list[Turn]:
    turns = []
    for t in items or []:
        if not isinstance(t, dict):
            continue
        role = "agent" if t.get("role") == "agent" else "candidate"
        text = (t.get("message") or "").strip()
        if role == "agent":
            text = _VOICE_TAG.sub("", text).strip()
        if text:
            turns.append(Turn(role=role, text=text, t_s=t.get("time_in_call_secs")))
    return turns


def _start_time(data: dict) -> float | None:
    start = (data.get("metadata") or {}).get("start_time_unix_secs")
    return float(start) if isinstance(start, (int, float)) else None


def transcript_from_conversation(data: dict) -> InterviewTranscript:
    """Works for both the GET /conversations/{id} body and the webhook's `data` object."""
    meta = data.get("metadata") or {}
    start = _start_time(data)
    dur = int(meta.get("call_duration_secs") or 0)
    ended = datetime.fromtimestamp(start + dur, timezone.utc) if start else datetime.now(timezone.utc)
    return InterviewTranscript(
        conversation_id=str(data["conversation_id"]),
        turns=_to_turns(data.get("transcript") or []),
        duration_s=dur,
        ended_at=ended,
    )


def dynamic_variables_of(data: dict) -> dict:
    dv = (data.get("conversation_initiation_client_data") or {}).get("dynamic_variables")
    return dv if isinstance(dv, dict) else {}


@dataclass
class WebhookResult:
    transcript: InterviewTranscript
    dynamic_variables: dict = field(default_factory=dict)
    started_at_unix: float | None = None

    # Unverified, for logging only. Use client.identify(result) for anything that matters.
    @property
    def claimed_candidate_id(self) -> str | None:
        return self.dynamic_variables.get("candidate_id") or None


def parse_webhook(body: bytes) -> WebhookResult | None:
    """None for webhook types we ignore (e.g. post_call_audio). ValueError if malformed."""
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("payload is not an object")
        if payload.get("type") != "post_call_transcription":
            return None
        data = payload["data"]
        return WebhookResult(
            transcript=transcript_from_conversation(data),
            dynamic_variables=dynamic_variables_of(data),
            started_at_unix=_start_time(data),
        )
    except (KeyError, TypeError, AttributeError, UnicodeDecodeError) as e:
        raise ValueError(f"malformed webhook: {e!r}") from e


def verify_signature(secret: str, headers: dict, body: bytes, now: float | None = None) -> bool:
    """`ElevenLabs-Signature: t=<unix>,v0=<hex hmac_sha256(secret, f"{t}." + body)>`, 30-min tolerance.

    Works on raw bytes (no decode), accepts any of several v0 values (secret rotation), never raises.
    """
    header = next((v for k, v in headers.items() if k.lower() == "elevenlabs-signature"), None)
    if not header or not secret:
        return False
    ts, sigs = None, []
    for part in header.split(","):
        k, _, v = part.strip().partition("=")
        if k == "t":
            ts = v
        elif k == "v0" and v:
            sigs.append(v)
    if not ts or not ts.isdigit() or not sigs:
        return False
    if abs((now or time.time()) - int(ts)) > SIGNATURE_TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest().encode()
    return any(hmac.compare_digest(expected, s.encode("utf-8", "replace")) for s in sigs)


# ---------- clients ----------

def _join_url(agent_id: str, dv: dict) -> str:
    vars_b64 = base64.b64encode(json.dumps(dv).encode()).decode()
    return f"{TALK_TO_URL}?{urlencode({'agent_id': agent_id, 'vars': vars_b64})}"


class RealElevenLabsClient:
    def __init__(self, api_key: str, agent_id: str, webhook_secret: str | None,
                 base_url: str = "https://api.elevenlabs.io", token_secret: str | None = None,
                 ttl: timedelta = timedelta(hours=2)):
        self._agent_id = agent_id
        self._secret = webhook_secret or ""
        self.signer = InterviewSigner(token_secret or webhook_secret or api_key, ttl)
        self._http = httpx.AsyncClient(base_url=base_url, headers={"xi-api-key": api_key}, timeout=20)
        self._conv_by_candidate: dict[str, str] = {}
        self._checked: set[str] = set()  # finished conversations already inspected

    async def create_interview(self, candidate_id: str, questions: list[str], context: str,
                               *, job_id: str | None = None, candidate_name: str | None = None) -> InterviewSession:
        if not questions:
            raise ToolError("invalid_input", "at least one interview question is required")
        if not job_id:
            raise ToolError("invalid_input", "job_id is required so the transcript can be routed back")
        dv = build_dynamic_variables(candidate_id, questions, context, job_id, candidate_name)
        exp, token = self.signer.sign(candidate_id, job_id)
        dv |= {"interview_expires": str(exp), "interview_token": token}
        return InterviewSession(
            conversation_id=None,
            join_url=_join_url(self._agent_id, dv),
            expires_at=datetime.fromtimestamp(exp, timezone.utc),
        )

    async def _get(self, path: str, **params):
        try:
            r = await self._http.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.TransportError as e:
            raise ToolError("transient", f"elevenlabs network error: {e}") from e
        raise_for_status(r, "elevenlabs")
        data = json_body(r, "elevenlabs")
        if not isinstance(data, dict):
            raise ToolError("transient", "elevenlabs: unexpected response shape")
        return data

    async def get_transcript(self, conversation_id: str) -> InterviewTranscript | None:
        data = await self._get(f"/v1/convai/conversations/{path_id(conversation_id)}")
        status = data.get("status")
        if status == "failed":
            raise ToolError("fatal", f"elevenlabs conversation {conversation_id} failed")
        if status != "done":
            return None  # initiated / in-progress / processing
        return transcript_from_conversation(data)

    def identify(self, result: WebhookResult) -> tuple[str, str] | None:
        return self.signer.identify(result.dynamic_variables, result.started_at_unix)

    async def find_conversation(self, candidate_id: str, since: datetime) -> str | None:
        """Polling fallback: the first FINISHED conversation with a valid token for this candidate."""
        if candidate_id in self._conv_by_candidate:
            return self._conv_by_candidate[candidate_id]
        listing = await self._get("/v1/convai/conversations", agent_id=self._agent_id, page_size=30,
                                  call_start_after_unix=int(since.timestamp()) - 60)
        for conv in listing.get("conversations") or []:
            conv_id = str(conv.get("conversation_id") or "")
            if not conv_id or conv_id in self._checked:
                continue
            detail = await self._get(f"/v1/convai/conversations/{path_id(conv_id)}")
            if detail.get("status") not in ("done", "failed"):
                continue  # still live: look again next poll, don't pin it yet
            self._checked.add(conv_id)
            if detail.get("status") != "done":
                continue
            ident = self.signer.identify(dynamic_variables_of(detail), _start_time(detail))
            if ident:
                self._conv_by_candidate.setdefault(ident[0], conv_id)
        return self._conv_by_candidate.get(candidate_id)

    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        return verify_signature(self._secret, headers, body)


class MockElevenLabsClient:
    """Dev only: the transcript becomes available `delay_s` after create_interview (via the poller).

    It never accepts webhooks: a public instance left in mock mode must not let anyone POST a
    fake transcript.
    """

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
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
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

    def identify(self, result: WebhookResult) -> tuple[str, str] | None:
        return None

    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        return False


# ---------- polling fallback loop (no DB access: lane A supplies the callbacks) ----------

@dataclass
class PendingInterview:
    candidate_id: str
    conversation_id: str | None
    created_at: datetime


GetPending = Callable[[], Awaitable[list[PendingInterview]] | list[PendingInterview]]
OnDone = Callable[[str, InterviewTranscript], Awaitable[None] | None]
OnFailed = Callable[[str, ToolError], Awaitable[None] | None]


async def _maybe_await(x):
    return await x if inspect.isawaitable(x) else x


async def poll_interviews(client, get_pending: GetPending, on_done: OnDone,
                          interval_s: float = 10.0, stop: asyncio.Event | None = None,
                          on_failed: OnFailed | None = None) -> None:
    """Every `interval_s`: for each INTERVIEWING candidate, find its conversation and, when done,
    call `on_done(candidate_id, transcript)` (lane A emits InterviewDone there).

    - A candidate is only marked handled after on_done succeeds, so a DB hiccup is retried.
    - Each candidate is isolated: one failing conversation doesn't block the others.
    - A fatal error (e.g. the conversation failed) calls `on_failed` once (lane A can escalate),
      then that candidate is skipped.

    Start it at app startup when ELEVENLABS_POLL=true or in mock mode:
        asyncio.create_task(poll_interviews(get_tools().elevenlabs, get_pending, on_done))
    """
    handled: set[str] = set()
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            pending = await _maybe_await(get_pending())
        except Exception:
            log.exception("interview poll: get_pending failed")
            pending = []
        for p in pending:
            if p.candidate_id in handled:
                continue
            try:
                conv = p.conversation_id or await client.find_conversation(p.candidate_id, p.created_at)
                if not conv:
                    continue
                transcript = await client.get_transcript(conv)
                if transcript is None:
                    continue
                await _maybe_await(on_done(p.candidate_id, transcript))
                handled.add(p.candidate_id)
            except ToolError as e:
                if e.kind in ("fatal", "not_found", "invalid_input"):
                    handled.add(p.candidate_id)
                    log.warning("interview poll: giving up on %s: %r", p.candidate_id, e)
                    if on_failed:
                        try:
                            await _maybe_await(on_failed(p.candidate_id, e))
                        except Exception:
                            log.exception("interview poll: on_failed crashed")
                else:
                    log.warning("interview poll: %s will retry: %r", p.candidate_id, e)
            except Exception:  # never let the poller die mid-demo; retried next cycle
                log.exception("interview poll: error for %s; will retry", p.candidate_id)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
