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
    MockElevenLabsClient, PendingInterview, RealElevenLabsClient, parse_webhook, poll_interviews,
    verify_signature,
)

SECRET = "wsec_test"


def conversation(conv_id="conv_1", status="done", candidate_id="cand_1", job_id="job_1"):
    return {
        "conversation_id": conv_id, "agent_id": "agent_x", "status": status,
        "transcript": [
            {"role": "agent", "message": "Tell me about RAG.", "time_in_call_secs": 0},
            {"role": "user", "message": "I built a RAG tutor.", "time_in_call_secs": 4},
            {"role": "agent", "message": None, "time_in_call_secs": 9},  # tool-call turns have no text
        ],
        "metadata": {"start_time_unix_secs": 1_760_000_000, "call_duration_secs": 95},
        "conversation_initiation_client_data": {
            "dynamic_variables": {"candidate_id": candidate_id, "job_id": job_id, "system__time": "x"}},
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
    assert r.candidate_id == "cand_1" and r.job_id == "job_1"
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
