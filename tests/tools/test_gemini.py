"""Gemini provider for the shared LLM wrapper (offline: the google-genai client is faked)."""
import types as pytypes

import pytest
from google.genai import errors
from google.genai import types as gtypes
from pydantic import BaseModel, Field

from api.config import Settings
from api.tools import build_tools
from api.tools.base import ToolError
from api.tools.llm import LLM, GeminiLLM, resolve_model


class Out(BaseModel):
    score: int = Field(ge=0, le=100)
    note: str


def gemini_with(behavior) -> tuple[GeminiLLM, list]:
    calls = []

    async def generate_content(*, model, contents, config):
        calls.append({"model": model, "contents": contents, "config": config})
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    llm = GeminiLLM("k", "gemini-test")
    llm._client = pytypes.SimpleNamespace(aio=pytypes.SimpleNamespace(
        models=pytypes.SimpleNamespace(generate_content=generate_content)))
    return llm, calls


def resp(text='{"score": 80, "note": "ok"}', finish="STOP", block=None, candidates=True):
    return pytypes.SimpleNamespace(
        text=text,
        prompt_feedback=pytypes.SimpleNamespace(block_reason=block) if block else None,
        candidates=[pytypes.SimpleNamespace(finish_reason=getattr(gtypes.FinishReason, finish))] if candidates else [],
    )


def api_error(cls, code, message="boom"):
    return cls(code, {"error": {"code": code, "message": message, "status": "X"}})


# ---------- provider routing ----------

def test_llm_factory_returns_gemini_and_maps_claude_model_ids():
    s = Settings(_env_file=None, llm_provider="gemini", gemini_api_key="k", gemini_model="gemini-x")
    llm = LLM("anthropic-key-ignored", "claude-sonnet-5-5", settings=s)  # exactly how lane A calls it
    assert isinstance(llm, GeminiLLM) and llm.model == "gemini-x"
    assert resolve_model(s, "gemini-custom") == "gemini-custom"


def test_explicit_anthropic_provider_wins():
    s = Settings(_env_file=None, llm_provider="gemini")
    llm = LLM("x", "claude-sonnet-5-5", provider="anthropic", settings=s)
    assert type(llm) is LLM and llm.model == "claude-sonnet-5-5"


def test_build_tools_uses_gemini_for_every_llm_tool():
    t = build_tools(Settings(_env_file=None, llm_provider="gemini", gemini_api_key="k", tools_mode="mock"))
    assert isinstance(t.drafter._llm, GeminiLLM) and isinstance(t.evaluator._llm, GeminiLLM)
    assert t.status["evaluator"] == "real (gemini: gemini-3.8-flash)"


# ---------- request shape + parsing ----------

async def test_structured_sends_json_schema_and_parses():
    llm, calls = gemini_with(resp())
    out = await llm.structured(system="sys", prompt="p", output=Out, max_tokens=1000)
    assert out == Out(score=80, note="ok")
    cfg = calls[0]["config"]
    assert cfg.system_instruction == "sys" and cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema["properties"]["score"]["maximum"] == 100
    assert cfg.max_output_tokens >= 8192  # headroom for thinking tokens
    assert calls[0]["model"] == "gemini-test"


@pytest.mark.parametrize("r,kind", [
    (resp(text='{"score": 105, "note": "x"}'), "transient"),   # out of bounds -> schema error
    (resp(text='{"score": 8'), "transient"),                    # truncated JSON
    (resp(finish="MAX_TOKENS"), "transient"),
    (resp(candidates=False), "transient"),
    (resp(finish="SAFETY"), "fatal"),
    (resp(block="SAFETY"), "fatal"),
])
async def test_bad_outputs_map_to_tool_errors(r, kind):
    llm, _ = gemini_with(r)
    with pytest.raises(ToolError) as e:
        await llm.structured(system="s", prompt="p", output=Out)
    assert e.value.kind == kind


@pytest.mark.parametrize("err,kind", [
    (api_error(errors.ClientError, 429, "quota"), "rate_limited"),
    (api_error(errors.ClientError, 403, "permission"), "fatal"),
    (api_error(errors.ClientError, 404, "model not found"), "fatal"),
    (api_error(errors.ClientError, 400, "API key not valid"), "fatal"),
    (api_error(errors.ClientError, 400, "bad schema"), "invalid_input"),
    (api_error(errors.ServerError, 503, "overloaded"), "transient"),
    (TimeoutError("slow"), "transient"),
])
async def test_api_errors_map_to_tool_errors(err, kind):
    llm, _ = gemini_with(err)
    with pytest.raises(ToolError) as e:
        await llm.structured(system="s", prompt="p", output=Out)
    assert e.value.kind == kind


async def test_lane_b_tools_run_on_gemini_end_to_end():
    """Drafter through the Gemini wrapper (fake client)."""
    from api.tools.drafter import Drafter
    llm, calls = gemini_with(resp(text='{"subject": "Hi", "body": "Hello Sarah, quick chat?"}'))
    d = await Drafter(llm).outreach({"title": "AI Engineer"}, {"profile": {"name": "Sarah"}})
    assert d.body.startswith("Hello Sarah") and "<candidate_memory>" in calls[0]["contents"]


def test_429_retry_delay_parsed_from_retryinfo_and_message():
    from api.tools.llm import _gemini_error
    e = errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED",
                                           "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                        "retryDelay": "28s"}]}})
    assert _gemini_error(e).retry_after_s == 28.0
    e = errors.ClientError(429, {"error": {"code": 429, "message": "Quota exceeded. Please retry in 27.1s.",
                                           "status": "RESOURCE_EXHAUSTED"}})
    t = _gemini_error(e)
    assert t.kind == "rate_limited" and t.retry_after_s == pytest.approx(27.1)
