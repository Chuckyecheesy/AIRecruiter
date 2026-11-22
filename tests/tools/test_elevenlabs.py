import asyncio
import base64
import hashlib
import hmac
import json
import sys
import time
import types
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.tools.base import ToolError
from api.tools.elevenlabs import (
    InterviewSigner, MockElevenLabsClient, PendingInterview, RealElevenLabsClient, parse_webhook,
    poll_interviews, verify_signature,
)

SECRET = "wsec_test"


SIGNER = InterviewSigner(SECRET)  # RealElevenLabsClient(…, SECRET) signs with the webhook secret


def signed_vars(candidate_id="cand_1", job_id="job_1", token=None) -> dict:
    exp, tok = SIGNER.sign(candidate_id, job_id)
    return {"candidate_id": candidate_id, "job_id": job_id, "interview_expires": str(exp),
            "interview_token": tok if token is None else token, "system__time": "x"}


def conversation(conv_id="conv_1", status="done", candidate_id="cand_1", job_id="job_1", token=None):
    return {
        "conversation_id": conv_id, "agent_id": "agent_x", "status": status,
        "transcript": [
            {"role": "agent", "message": "Tell me about RAG.", "time_in_call_secs": 0},
            {"role": "user", "message": "I built a RAG tutor.", "time_in_call_secs": 4},
            {"role": "agent", "message": None, "time_in_call_secs": 9},  # tool-call turns have no text
        ],
        "metadata": {"start_time_unix_secs": int(time.time()) - 120, "call_duration_secs": 95},
        "conversation_initiation_client_data": {
            "dynamic_variables": signed_vars(candidate_id, job_id, token)},
    }


def sign(body: bytes, ts: int | None = None, secret: str = SECRET) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v0={mac}"


def webhook_body(**kw) -> bytes:
    return json.dumps({"type": "post_call_transcription", "event_timestamp": 1, "data": conversation(**kw)}).encode()


def real_client(handler) -> RealElevenLabsClient:
    c = RealElevenLabsClient("xi_key", "agent_x", SECRET)
    c._http = httpx.AsyncClient(base_url="https://api.elevenlabs.io", headers={"xi-api-key": "xi_key"},
                                transport=httpx.MockTransport(handler))
    return c


# ---------- signature ----------

def test_signature_valid_and_case_insensitive_header():
    body = webhook_body()
    assert verify_signature(SECRET, {"ElevenLabs-Signature": sign(body)}, body)
    assert verify_signature(SECRET, {"elevenlabs-signature": sign(body)}, body)


def test_signature_rejects_tamper_stale_wrong_secret_missing():
    body = webhook_body()
    assert not verify_signature(SECRET, {"elevenlabs-signature": sign(body)}, body + b" ")
    assert not verify_signature(SECRET, {"elevenlabs-signature": sign(body, ts=int(time.time()) - 3600)}, body)
    assert not verify_signature(SECRET, {"elevenlabs-signature": sign(body, secret="other")}, body)
    assert not verify_signature(SECRET, {}, body)
    assert not verify_signature("", {"elevenlabs-signature": sign(body)}, body)


# ---------- parsing ----------

def test_parse_webhook_maps_roles_and_dynamic_variables():
    r = parse_webhook(webhook_body())
    assert r.claimed_candidate_id == "cand_1"
    assert real_client(lambda q: httpx.Response(500)).identify(r) == ("cand_1", "job_1")
    assert [t.role for t in r.transcript.turns] == ["agent", "candidate"]  # empty turn dropped
    assert r.transcript.duration_s == 95


def test_parse_webhook_ignores_other_types():
    assert parse_webhook(json.dumps({"type": "post_call_audio", "data": {}}).encode()) is None


# ---------- real client ----------

async def test_create_interview_builds_talk_to_link():
    c = real_client(lambda req: httpx.Response(500))
    s = await c.create_interview("cand_1", ["Q one", "Q two"], "ctx", job_id="job_1", candidate_name="Sarah")
    q = parse_qs(urlparse(s.join_url).query)
    assert s.join_url.startswith("https://elevenlabs.io/app/talk-to?")
    assert q["agent_id"] == ["agent_x"]
    dv = json.loads(base64.b64decode(q["vars"][0]))
    assert dv["candidate_id"] == "cand_1" and dv["job_id"] == "job_1" and dv["candidate_name"] == "Sarah"
    assert dv["interview_questions"] == "1. Q one\n2. Q two"
    assert s.conversation_id is None


async def test_create_interview_requires_questions():
    with pytest.raises(ToolError) as e:
        await real_client(lambda r: httpx.Response(500)).create_interview("c", [], "")
    assert e.value.kind == "invalid_input"


async def test_get_transcript_status_handling():
    statuses = {"conv_done": "done", "conv_live": "in-progress", "conv_bad": "failed"}

    def handler(req: httpx.Request):
        assert req.headers["xi-api-key"] == "xi_key"
        cid = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=conversation(conv_id=cid, status=statuses[cid]))

    c = real_client(handler)
    assert (await c.get_transcript("conv_done")).conversation_id == "conv_done"
    assert await c.get_transcript("conv_live") is None
    with pytest.raises(ToolError) as e:
        await c.get_transcript("conv_bad")
    assert e.value.kind == "fatal"


async def test_http_errors_map_to_tool_errors():
    c = real_client(lambda r: httpx.Response(429, headers={"retry-after": "3"}))
    with pytest.raises(ToolError) as e:
        await c.get_transcript("x")
    assert e.value.kind == "rate_limited" and e.value.retry_after_s == 3.0


async def test_find_conversation_matches_dynamic_variable():
    def handler(req: httpx.Request):
        if req.url.path == "/v1/convai/conversations":
            assert req.url.params["agent_id"] == "agent_x"
            return httpx.Response(200, json={"conversations": [
                {"conversation_id": "conv_a"}, {"conversation_id": "conv_b"}], "has_more": False})
        cid = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=conversation(conv_id=cid, candidate_id={"conv_a": "other", "conv_b": "cand_1"}[cid]))

    c = real_client(handler)
    assert await c.find_conversation("cand_1", datetime.now(timezone.utc)) == "conv_b"
    assert await c.find_conversation("nobody", datetime.now(timezone.utc)) is None


# ---------- poller ----------

async def test_poller_calls_on_done_once():
    client = MockElevenLabsClient("http://x", delay_s=0)
    s = await client.create_interview("cand_1", ["q"], "")
    done: list[str] = []
    stop = asyncio.Event()

    async def on_done(cid, transcript):
        done.append(cid)

    task = asyncio.create_task(poll_interviews(
        client, lambda: [PendingInterview("cand_1", None, datetime.now(timezone.utc))], on_done,
        interval_s=0.01, stop=stop))
    await asyncio.sleep(0.08)
    stop.set()
    await task
    assert done == ["cand_1"]
    assert s.join_url.endswith(f"/api/webhooks/mock-interview/{s.conversation_id}")


# ---------- route ----------

@pytest.fixture
def app_with_fake_repo(monkeypatch):
    from api.config import Settings
    from api.routes import webhooks
    from api.tools import build_tools

    tools = build_tools(Settings(_env_file=None, llm_mode="stub", tools_mode="mock"))
    tools.elevenlabs = RealElevenLabsClient("xi", "agent_x", SECRET)
    monkeypatch.setattr(webhooks, "get_tools", lambda: tools)
    from collections import OrderedDict
    monkeypatch.setattr(webhooks, "_seen_conversations", OrderedDict())

    emitted: list[tuple] = []
    repo = types.ModuleType("api.db.repo")
    repo.emit_event = lambda type, **kw: emitted.append((type, kw))
    db = types.ModuleType("api.db")
    db.repo = repo
    monkeypatch.setitem(sys.modules, "api.db", db)
    monkeypatch.setitem(sys.modules, "api.db.repo", repo)

    app = FastAPI()
    app.include_router(webhooks.router)
    return TestClient(app), emitted


def test_webhook_route_emits_interview_done(app_with_fake_repo):
    client, emitted = app_with_fake_repo
    body = webhook_body()
    r = client.post("/api/webhooks/elevenlabs", content=body, headers={"ElevenLabs-Signature": sign(body)})
    assert r.status_code == 200, r.text
    (etype, kw), = emitted
    assert etype == "InterviewDone" and kw["candidate_id"] == "cand_1" and kw["job_id"] == "job_1"
    assert kw["payload"]["conversation_id"] == "conv_1" and kw["payload"]["transcript"]["turns"]


def test_webhook_route_rejects_bad_signature(app_with_fake_repo):
    client, emitted = app_with_fake_repo
    r = client.post("/api/webhooks/elevenlabs", content=webhook_body(), headers={"ElevenLabs-Signature": "t=1,v0=00"})
    assert r.status_code == 401 and not emitted


def test_webhook_route_ignores_foreign_conversations(app_with_fake_repo):
    client, emitted = app_with_fake_repo
    body = webhook_body(candidate_id="", job_id="")
    r = client.post("/api/webhooks/elevenlabs", content=body, headers={"ElevenLabs-Signature": sign(body)})
    assert r.status_code == 200 and r.json()["ignored"] and not emitted


# ---------- interview token (security fix H2) ----------

def test_identify_rejects_tampered_or_missing_token():
    c = real_client(lambda q: httpx.Response(500))
    assert c.identify(parse_webhook(webhook_body(token="0" * 32))) is None
    body = json.loads(webhook_body())
    body["data"]["conversation_initiation_client_data"]["dynamic_variables"]["candidate_id"] = "someone_else"
    assert c.identify(parse_webhook(json.dumps(body).encode())) is None  # token bound to the ids
    body["data"]["conversation_initiation_client_data"]["dynamic_variables"] = {"candidate_id": "x", "job_id": "y"}
    assert c.identify(parse_webhook(json.dumps(body).encode())) is None


def test_identify_rejects_call_started_after_expiry():
    signer = InterviewSigner(SECRET)
    exp, tok = signer.sign("c", "j", now=time.time() - 3 * 3600)  # expired an hour ago
    dv = {"candidate_id": "c", "job_id": "j", "interview_expires": str(exp), "interview_token": tok}
    assert signer.identify(dv, started_at_unix=time.time()) is None
    assert signer.identify(dv, started_at_unix=exp - 10) == ("c", "j")


async def test_create_interview_vars_carry_valid_token():
    c = real_client(lambda q: httpx.Response(500))
    s = await c.create_interview("cand_1", ["q"], "ctx", job_id="job_1")
    dv = json.loads(base64.b64decode(parse_qs(urlparse(s.join_url).query)["vars"][0]))
    assert c.signer.identify(dv, time.time()) == ("cand_1", "job_1")


async def test_create_interview_requires_job_id():
    with pytest.raises(ToolError) as e:
        await real_client(lambda q: httpx.Response(500)).create_interview("c", ["q"], "")
    assert e.value.kind == "invalid_input"


def test_mock_client_never_accepts_webhooks():
    body = webhook_body()
    assert MockElevenLabsClient("http://x").verify_webhook({"ElevenLabs-Signature": sign(body)}, body) is False


def test_signature_accepts_any_of_multiple_v0_and_never_raises():
    body = webhook_body()
    good = sign(body).split(",")[1]
    ts = sign(body).split(",")[0]
    assert verify_signature(SECRET, {"elevenlabs-signature": f"{ts},v0=deadbeef,{good}"}, body)
    assert not verify_signature(SECRET, {"elevenlabs-signature": f"{ts},v0=ünïcode"}, body)
    assert not verify_signature(SECRET, {"elevenlabs-signature": "garbage"}, b"\xff\xfe")


def test_webhook_route_rejects_forged_token_and_dedupes(app_with_fake_repo):
    client, emitted = app_with_fake_repo
    forged = webhook_body(conv_id="conv_forged", token="f" * 32)
    r = client.post("/api/webhooks/elevenlabs", content=forged, headers={"ElevenLabs-Signature": sign(forged)})
    assert r.status_code == 200 and r.json()["ignored"] and not emitted
    body = webhook_body(conv_id="conv_dup")
    for _ in range(2):
        client.post("/api/webhooks/elevenlabs", content=body, headers={"ElevenLabs-Signature": sign(body)})
    assert len(emitted) == 1


def test_webhook_route_malformed_and_oversized(app_with_fake_repo, monkeypatch):
    client, emitted = app_with_fake_repo
    bad = b'{"type": "post_call_transcription"}'  # signed but missing data
    assert client.post("/api/webhooks/elevenlabs", content=bad,
                       headers={"ElevenLabs-Signature": sign(bad)}).status_code == 400
    from api.config import Settings
    from api.routes import webhooks
    monkeypatch.setattr(webhooks, "get_settings", lambda: Settings(_env_file=None, webhook_max_bytes=100))
    big = b"x" * 500
    assert client.post("/api/webhooks/elevenlabs", content=big).status_code == 413


def test_transcript_strips_voice_tags_and_blank_turns():
    from api.tools.elevenlabs import _to_turns
    turns = _to_turns([
        {"role": "agent", "message": "Great, let's dive in. [friendly] Walk me through a RAG system.", "time_in_call_secs": 1},
        {"role": "agent", "message": "  \n", "time_in_call_secs": 2},
        {"role": "user", "message": "I used [brackets] in my answer", "time_in_call_secs": 3},
    ])
    assert [t.text for t in turns] == ["Great, let's dive in. Walk me through a RAG system.",
                                       "I used [brackets] in my answer"]  # candidate text untouched


# ---------- integration with lane A's real repo (catches signature drift) ----------

def test_webhook_route_emits_into_real_repo(tmp_path, monkeypatch):
    from collections import OrderedDict

    from api.config import Settings
    from api.db import repo
    from api.routes import webhooks
    from api.tools import build_tools

    repo.init_db(tmp_path / "recruiter.db")
    job_id = repo.create_job("AI Engineer", {"title": "AI Engineer"})
    from api.contracts import JobState
    repo.set_job_state(job_id, JobState.ACTIVE)  # the dispatcher only sees ACTIVE jobs' events
    cand_id = repo.create_candidate(job_id, "pl_001", "Sarah Chen")

    tools = build_tools(Settings(_env_file=None, llm_mode="stub", tools_mode="mock"))
    tools.elevenlabs = RealElevenLabsClient("xi", "agent_x", SECRET)
    monkeypatch.setattr(webhooks, "get_tools", lambda: tools)
    monkeypatch.setattr(webhooks, "_seen_conversations", OrderedDict())
    app = FastAPI()
    app.include_router(webhooks.router)

    body = webhook_body(conv_id="conv_real", candidate_id=cand_id, job_id=job_id)
    r = TestClient(app).post("/api/webhooks/elevenlabs", content=body, headers={"ElevenLabs-Signature": sign(body)})
    assert r.status_code == 200 and r.json() == {"ok": True}, r.text

    events = [e for e in repo.fetch_due_events(limit=50, job_id=job_id) if e.type.value == "InterviewDone"]
    assert len(events) == 1
    ev = events[0]
    assert ev.candidate_id == cand_id and ev.payload["conversation_id"] == "conv_real"
    assert ev.payload["transcript"]["turns"][0]["role"] == "agent"
