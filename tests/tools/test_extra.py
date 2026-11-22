"""Extra lane B coverage: error mapping, real clients via MockTransport, config, LLM wrapper,
evaluator/rubric math, persona, mock ElevenLabs timing, poller resilience, webhook 503, fixtures.

All offline. (13 bugs these tests originally caught as strict xfails are now fixed.)
"""
import asyncio
import json
import re
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.config import Settings
from api.tools import build_tools
from api.tools.base import FailingTool, ToolError
from api.tools.browser import DisabledBrowserTool
from api.tools.elevenlabs import (
    MockElevenLabsClient, PendingInterview, RealElevenLabsClient, poll_interviews,
)
from api.tools.email_sim import EmailSimulator
from api.tools.evaluator import _recompute
from api.tools.fiber import DemoInboxFiber, MockFiberClient, RealFiberClient, demo_address
from api.tools.llm import LLM, to_jsonable
from api.tools.models import (
    ContactInfo, CriterionScore, EnrichedProfile, Evaluation, InterviewTranscript, Message, RoleForm,
    Rubric, RubricCriterion, SearchCriteria, Turn,
)
from api.tools.persona import LLMCandidatePersona
from api.tools.ploid import MockPloidClient, RealPloidClient, _raise_for_status
from api.tools.rubric import RubricGenerator, normalize

ROOT = Path(__file__).resolve().parents[2]
REQ = httpx.Request("GET", "https://x.test/")


def S(**kw) -> Settings:
    base = dict(_env_file=None, tools_mode="mock", llm_mode="stub", anthropic_api_key=None,
                ploid_mode=None, fiber_mode=None, elevenlabs_mode=None, browser_mode=None, fail_tool=None,
                ploid_api_key=None, ploid_base_url=None, fiber_api_key=None, fiber_base_url=None,
                elevenlabs_api_key=None, elevenlabs_agent_id=None, elevenlabs_webhook_secret=None,
                elevenlabs_poll=False, demo_inbox="demo@example.com", mock_interview_delay_s=0.0,
                allow_real_email=False, email_allowlist="")
    return Settings(**{**base, **kw})


def resp(status: int, headers: dict | None = None, text: str = "") -> httpx.Response:
    return httpx.Response(status, headers=headers or {}, text=text, request=REQ)


# ======================= _raise_for_status kind mapping =======================

@pytest.mark.parametrize("status", [200, 201, 204, 302, 399])
def test_raise_for_status_ok_below_400(status):
    _raise_for_status(resp(status), "ploid")


@pytest.mark.parametrize("status,kind", [
    (404, "not_found"), (400, "invalid_input"), (422, "invalid_input"),
    (401, "fatal"), (403, "fatal"),
    (500, "transient"), (502, "transient"), (503, "transient"), (504, "transient"),
    (408, "transient"), (409, "transient"), (418, "transient"),
])
def test_raise_for_status_kinds(status, kind):
    with pytest.raises(ToolError) as e:
        _raise_for_status(resp(status, text="boom"), "ploid")
    assert e.value.kind == kind
    assert "ploid" in str(e.value)


def test_raise_for_status_429_with_retry_after():
    with pytest.raises(ToolError) as e:
        _raise_for_status(resp(429, {"retry-after": "12"}), "fiber")
    assert e.value.kind == "rate_limited" and e.value.retry_after_s == 12.0


def test_raise_for_status_429_without_retry_after_defaults_5():
    with pytest.raises(ToolError) as e:
        _raise_for_status(resp(429), "fiber")
    assert e.value.kind == "rate_limited" and e.value.retry_after_s == 5.0


def test_raise_for_status_429_fractional_retry_after():
    with pytest.raises(ToolError) as e:
        _raise_for_status(resp(429, {"Retry-After": "1.5"}), "fiber")
    assert e.value.retry_after_s == 1.5


def test_raise_for_status_429_http_date_retry_after():
    with pytest.raises(ToolError) as e:
        _raise_for_status(resp(429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}), "ploid")
    assert e.value.kind == "rate_limited"


def test_invalid_input_message_truncates_body():
    with pytest.raises(ToolError) as e:
        _raise_for_status(resp(400, text="x" * 1000), "ploid")
    assert len(str(e.value)) < 260


def test_toolerror_repr_and_default_message():
    e = ToolError("fatal")
    assert str(e) == "fatal" and "kind='fatal'" in repr(e) and e.retry_after_s is None


# ======================= RealPloidClient via MockTransport =======================

def ploid_with(handler) -> RealPloidClient:
    c = RealPloidClient("pk_test", "https://ploid.test")
    c._http = httpx.AsyncClient(base_url="https://ploid.test", headers={"Authorization": "Bearer pk_test"},
                                transport=httpx.MockTransport(handler))
    return c


async def test_real_ploid_search_sends_criteria_and_limit():
    seen = {}

    def h(req: httpx.Request):
        seen["method"], seen["path"] = req.method, req.url.path
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"results": [{"ploid_id": "p1", "name": "A"}]})

    c = ploid_with(h)
    out = await c.search(SearchCriteria(title="AI Eng", must_have_skills=["RAG"]), limit=7)
    assert [x.ploid_id for x in out] == ["p1"]
    assert seen["method"] == "POST" and seen["path"] == "/search" and seen["auth"] == "Bearer pk_test"
    assert seen["body"]["limit"] == 7 and seen["body"]["title"] == "AI Eng"


def test_real_ploid_constructor_sets_auth_header():
    c = RealPloidClient("pk_abc", "https://ploid.test")
    assert c._http.headers["authorization"] == "Bearer pk_abc"
    assert str(c._http.base_url).startswith("https://ploid.test")


async def test_real_ploid_search_accepts_bare_list():
    c = ploid_with(lambda r: httpx.Response(200, json=[{"ploid_id": "p1", "name": "A"}]))
    out = await c.search(SearchCriteria(title="x"), limit=1)
    assert out[0].ploid_id == "p1"


async def test_real_ploid_enrich_fills_defaults_and_completeness():
    def h(req):
        assert req.method == "GET" and req.url.path == "/profiles/p9"
        return httpx.Response(200, json={"name": "Z", "skills": ["py"], "links": {"github": "g"}})

    p = await ploid_with(h).enrich("p9")
    assert p.ploid_id == "p9" and p.completeness == pytest.approx(0.4)
    assert p.raw["name"] == "Z"


async def test_real_ploid_enrich_keeps_provided_completeness():
    c = ploid_with(lambda r: httpx.Response(200, json={"ploid_id": "p9", "name": "Z", "completeness": 0.9}))
    assert (await c.enrich("p9")).completeness == 0.9


@pytest.mark.parametrize("status,kind", [(404, "not_found"), (401, "fatal"), (422, "invalid_input"),
                                         (500, "transient"), (429, "rate_limited")])
async def test_real_ploid_http_errors(status, kind):
    c = ploid_with(lambda r: httpx.Response(status, text="err"))
    with pytest.raises(ToolError) as e:
        await c.enrich("p1")
    assert e.value.kind == kind


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout,
                                 httpx.RemoteProtocolError])
async def test_real_ploid_transport_errors_are_transient(exc):
    def h(req):
        raise exc("down", request=req)

    with pytest.raises(ToolError) as e:
        await ploid_with(h).search(SearchCriteria(title="x"), limit=1)
    assert e.value.kind == "transient"


async def test_real_ploid_non_json_200_maps_to_toolerror():
    c = ploid_with(lambda r: httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(ToolError):
        await c.enrich("p1")


# ======================= RealFiberClient + DemoInboxFiber =======================

def fiber_with(handler) -> RealFiberClient:
    c = RealFiberClient("fk", "https://fiber.test")
    c._http = httpx.AsyncClient(base_url="https://fiber.test", transport=httpx.MockTransport(handler))
    return c


async def test_real_fiber_success_and_request_shape():
    seen = {}

    def h(req):
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return httpx.Response(200, json={"email": "real@corp.com", "phone": "+1555", "confidence": 0.77})

    c = await fiber_with(h).find_contact("Sarah Chen", {"github": "g"}, "Acme")
    assert seen["path"] == "/contact/lookup"
    assert seen["body"] == {"name": "Sarah Chen", "links": {"github": "g"}, "company": "Acme"}
    assert c.email == "real@corp.com" and c.phone == "+1555" and c.confidence == 0.77 and c.source == "fiber"


async def test_real_fiber_default_confidence():
    c = await fiber_with(lambda r: httpx.Response(200, json={"phone": "+1"})).find_contact("a", {}, None)
    assert c.confidence == 0.5 and c.email is None


async def test_real_fiber_empty_result_is_not_found():
    with pytest.raises(ToolError) as e:
        await fiber_with(lambda r: httpx.Response(200, json={"email": None})).find_contact("a", {}, None)
    assert e.value.kind == "not_found"


@pytest.mark.parametrize("status,kind", [(404, "not_found"), (403, "fatal"), (503, "transient"),
                                         (400, "invalid_input")])
async def test_real_fiber_http_errors(status, kind):
    with pytest.raises(ToolError) as e:
        await fiber_with(lambda r: httpx.Response(status)).find_contact("a", {}, None)
    assert e.value.kind == kind


async def test_real_fiber_transport_error_transient():
    def h(req):
        raise httpx.ConnectError("x", request=req)

    with pytest.raises(ToolError) as e:
        await fiber_with(h).find_contact("a", {}, None)
    assert e.value.kind == "transient"


async def test_demo_inbox_rewrites_real_fiber_result():
    inner = fiber_with(lambda r: httpx.Response(200, json={"email": "real@corp.com", "phone": "+1555",
                                                           "confidence": 0.9}))
    c = await DemoInboxFiber(inner, "demo@example.com").find_contact("Sarah Chen", {}, None)
    assert c.email == "demo+sarah-chen@example.com" and c.phone is None
    assert c.confidence == 0.9 and c.source == "fiber"


async def test_demo_inbox_propagates_inner_errors():
    with pytest.raises(ToolError) as e:
        await DemoInboxFiber(fiber_with(lambda r: httpx.Response(404)), "d@x.io").find_contact("a", {}, None)
    assert e.value.kind == "not_found"


async def test_demo_inbox_phone_only_result_gets_demo_email():
    class Inner:
        async def find_contact(self, name, links, hint):
            return ContactInfo(phone="+1", source="x")

    c = await DemoInboxFiber(Inner(), "d@x.io").find_contact("Bo", {}, None)
    assert c.email == "d+bo@x.io" and c.phone is None


async def test_build_tools_never_exposes_real_email_from_any_fixture():
    t = build_tools(S())
    for pid in json.loads((ROOT / "fixtures/fiber_contacts.json").read_text()):
        p = await t.ploid.enrich(pid)
        c = await t.fiber.find_contact(p.name, p.links, None)
        assert c.email.startswith("demo+") and c.email.endswith("@example.com") and c.phone is None


# ======================= demo_address edge cases =======================

@pytest.mark.parametrize("tag,expected", [
    ("Sarah  Chen.", "demo+sarah-chen@example.com"),
    ("  O'Brien-Smith, Jr. ", "demo+o-brien-smith-jr@example.com"),
    ("ALLCAPS", "demo+allcaps@example.com"),
    ("a__b--c", "demo+a-b-c@example.com"),
    ("01JABCXYZ", "demo+01jabcxyz@example.com"),
])
def test_demo_address_slugging(tag, expected):
    assert demo_address("demo@example.com", tag) == expected


def test_demo_address_keeps_existing_plus_and_subdomain():
    assert demo_address("me+demo@mail.example.co.uk", "Bo") == "me+demo+bo@mail.example.co.uk"


def test_demo_address_unicode_is_deterministic():
    a = demo_address("demo@example.com", "José Núñez")
    assert a == demo_address("demo@example.com", "José Núñez")
    assert a.endswith("@example.com") and " " not in a


def test_demo_address_is_ascii():
    assert demo_address("demo@example.com", "José Núñez").isascii()


def test_demo_address_symbol_only_tag_not_empty():
    assert not demo_address("demo@example.com", "!!! ...").startswith("demo+@")


def test_demo_address_inbox_without_at():
    with pytest.raises(ValueError):
        demo_address("inbox", "Bo")


# ======================= MockFiber / MockPloid behavior =======================

async def test_mock_fiber_original_resolves_to_its_own_contact():
    p = await MockPloidClient().enrich("pl_001")
    c = await MockFiberClient().find_contact(p.name, p.links, None)
    assert c.confidence == 0.93


async def test_mock_fiber_resolves_by_name_without_links():
    c = await MockFiberClient().find_contact("priya RAMAN", {}, None)
    assert c.email == "priya.raman@example.com"


async def test_mock_ploid_blank_title_invalid_input():
    with pytest.raises(ToolError) as e:
        await MockPloidClient().search(SearchCriteria(title="   "), limit=3)
    assert e.value.kind == "invalid_input"


async def test_mock_ploid_limit_respected():
    m = MockPloidClient()
    assert len(await m.search(SearchCriteria(title="x"), limit=0)) == 0
    assert len(await m.search(SearchCriteria(title="x"), limit=3)) == 3
    assert len(await m.search(SearchCriteria(title="x"), limit=100)) == 8


# ======================= Settings + build_tools =======================

def test_mode_for_defaults_and_overrides():
    s = S(tools_mode="real")
    assert s.mode_for("ploid") == "real" and s.mode_for("fiber") == "real"
    assert s.mode_for("browser") == "off"
    s = S(tools_mode="real", fiber_mode="mock", browser_mode="real", elevenlabs_mode="off")
    assert s.mode_for("fiber") == "mock" and s.mode_for("browser") == "real"
    assert s.mode_for("elevenlabs") == "off" and s.mode_for("ploid") == "real"
    assert S(tools_mode="mock", ploid_mode="real").mode_for("ploid") == "real"
    assert S(tools_mode="mock").mode_for("browser") == "off"


def test_email_allowlist_parsing():
    assert S(email_allowlist=" A@x.io, ,b@Y.io ,").email_allowlist_set == {"a@x.io", "b@y.io"}
    assert S().email_allowlist_set == set()


def test_llm_enabled_flag():
    assert S(llm_mode="real").llm_enabled and not S(llm_mode="stub").llm_enabled


def test_build_tools_real_mode_without_keys_falls_back_to_mock():
    t = build_tools(S(tools_mode="real"))
    # falls back to mock, but /health says why (security fix: no silent fallback)
    for tool in ("ploid", "fiber", "elevenlabs"):
        assert t.status[tool].startswith("mock (real requested; missing")
    assert isinstance(t.ploid, MockPloidClient)


def test_build_tools_needs_both_key_and_url():
    t = build_tools(S(tools_mode="real", ploid_api_key="k", fiber_base_url="https://f"))
    assert t.status["ploid"] == "mock (real requested; missing PLOID_BASE_URL)"
    assert t.status["fiber"] == "mock (real requested; missing FIBER_API_KEY)"
    assert isinstance(t.ploid, MockPloidClient)


def test_build_tools_real_clients_when_keys_present():
    t = build_tools(S(tools_mode="real", ploid_api_key="k", ploid_base_url="https://p",
                      fiber_api_key="k", fiber_base_url="https://f",
                      elevenlabs_api_key="xi", elevenlabs_agent_id="ag", elevenlabs_poll=True))
    assert isinstance(t.ploid, RealPloidClient) and t.status["ploid"] == "real"
    assert isinstance(t.fiber, DemoInboxFiber) and isinstance(t.fiber._inner, RealFiberClient)
    assert isinstance(t.elevenlabs, RealElevenLabsClient) and t.status["elevenlabs"] == "real (poll)"


def test_build_tools_elevenlabs_webhook_status_and_per_tool_override():
    t = build_tools(S(tools_mode="mock", elevenlabs_mode="real", elevenlabs_api_key="xi",
                      elevenlabs_agent_id="ag", elevenlabs_webhook_secret="ws",
                      ploid_api_key="k", ploid_base_url="https://p"))
    assert t.status["elevenlabs"] == "real (webhook)"
    t = build_tools(S(elevenlabs_mode="real", elevenlabs_api_key="xi", elevenlabs_agent_id="ag"))
    assert t.status["elevenlabs"] == "real (webhook) (no webhook secret!)"
    assert t.status["ploid"] == "mock"  # keys present but mode is mock


def test_build_tools_mock_mode_ignores_keys():
    t = build_tools(S(tools_mode="mock", ploid_api_key="k", ploid_base_url="https://p"))
    assert isinstance(t.ploid, MockPloidClient)


async def test_browser_off_by_default():
    t = build_tools(S(tools_mode="real"))
    assert t.status["browser"] == "off" and isinstance(t.browser, DisabledBrowserTool)
    assert t.browser.enabled is False
    with pytest.raises(ToolError) as e:
        await t.browser.research(["https://x"], ["q"])
    assert e.value.kind == "fatal"


def test_build_tools_llm_status_real_when_enabled():
    t = build_tools(S(llm_mode="real", anthropic_api_key="sk-test"))  # constructs client, no call
    assert t.status["drafter"] == t.status["evaluator"] == t.status["rubric"] == t.status["persona"] == "real"


def test_mock_elevenlabs_gets_public_base_url():
    t = build_tools(S(public_base_url="https://demo.app/", mock_interview_delay_s=3.0))
    assert t.elevenlabs._base == "https://demo.app" and t.elevenlabs._delay == 3.0


# ======================= FAIL_TOOL =======================

def test_fail_tool_invalid_name_raises():
    with pytest.raises(ValueError, match="FAIL_TOOL"):
        build_tools(S(fail_tool="nonexistent"))


async def test_fail_tool_case_and_whitespace_insensitive():
    t = build_tools(S(fail_tool="  PLOID "))
    assert "FAIL_TOOL" in t.status["ploid"]
    with pytest.raises(ToolError) as e:
        await t.ploid.enrich("pl_001")
    assert e.value.kind == "transient"


@pytest.mark.parametrize("name", ["email", "drafter", "evaluator", "elevenlabs", "persona", "classifier",
                                  "rubric", "browser"])
def test_fail_tool_accepts_every_tool(name):
    t = build_tools(S(fail_tool=name))
    assert isinstance(getattr(t, name), FailingTool)


def test_fail_tool_rejects_status_field():
    with pytest.raises(ValueError):
        build_tools(S(fail_tool="status"))


def test_failing_tool_passes_through_non_async_attrs():
    t = build_tools(S(fail_tool="elevenlabs"))
    assert t.elevenlabs.verify_webhook({}, b"") is False  # sync method passes through (mock rejects all)
    t = build_tools(S(fail_tool="browser"))
    assert t.browser.enabled is False


async def test_failing_tool_wraps_all_async_methods_and_keeps_names():
    t = build_tools(S(fail_tool="elevenlabs"))
    for coro in (t.elevenlabs.create_interview("c", ["q"], "x"), t.elevenlabs.get_transcript("c"),
                 t.elevenlabs.find_conversation("c", datetime.now(timezone.utc))):
        with pytest.raises(ToolError) as e:
            await coro
        assert e.value.kind == "transient"
    assert t.elevenlabs.get_transcript.__name__ == "get_transcript"


def test_failing_tool_missing_attr_raises_attribute_error():
    with pytest.raises(AttributeError):
        FailingTool(object(), "x").nope


# ======================= Email simulator =======================

@pytest.mark.parametrize("to,body", [("", "b"), ("no-at-sign", "b"), ("a@x.io", "   \n"), (None, "b")])
async def test_email_invalid_input(to, body):
    with pytest.raises(ToolError) as e:
        await EmailSimulator().send(to, "s", body, "t")
    assert e.value.kind == "invalid_input"


async def test_email_allow_real_but_not_allowlisted_stays_simulated():
    r = await EmailSimulator(True, {"me@x.io"}).send("other@x.io", "s", "b", "t1")
    assert r.simulated and r.thread_id == "t1" and r.to == "other@x.io"


async def test_email_allowlisted_without_allow_flag_stays_simulated():
    r = await EmailSimulator(False, {"me@x.io"}).send("me@x.io", "s", "b", "t")
    assert r.simulated


async def test_email_guard_case_insensitive_via_build_tools():
    t = build_tools(S(allow_real_email=True, email_allowlist="Me@X.io"))
    with pytest.raises(ToolError) as e:
        await t.email.send("ME@x.IO", "s", "b", "t")
    assert e.value.kind == "fatal"


async def test_email_message_ids_unique():
    e = EmailSimulator()
    ids = {(await e.send("a@x.io", "s", "b", "t")).message_id for _ in range(20)}
    assert len(ids) == 20


# ======================= LLM wrapper (fake client, no network) =======================

class _FakeMessages:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    async def parse(self, **kw):
        self.calls.append(kw)
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        return self.behavior


def fake_llm(behavior) -> LLM:
    llm = LLM("sk-test-not-real", "claude-test")
    llm._client = types.SimpleNamespace(messages=_FakeMessages(behavior))
    return llm


def api_err(cls, status, message="err", headers=None):
    r = httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", "https://api.anthropic.com"))
    return cls(message, response=r, body=None)


class _Out(RubricCriterion):
    pass


async def test_llm_success_passes_params():
    llm = fake_llm(types.SimpleNamespace(stop_reason="end_turn", parsed_output="OK"))
    out = await llm.structured(system="sys", prompt="p", output=_Out, max_tokens=123, effort="medium")
    assert out == "OK"
    kw = llm._client.messages.calls[0]
    assert kw["model"] == "claude-test" and kw["max_tokens"] == 123 and kw["system"] == "sys"
    assert kw["messages"] == [{"role": "user", "content": "p"}] and kw["output_format"] is _Out
    assert kw["output_config"] == {"effort": "medium"}


@pytest.mark.parametrize("exc,kind", [
    (lambda: api_err(anthropic.AuthenticationError, 401), "fatal"),
    (lambda: api_err(anthropic.PermissionDeniedError, 403), "fatal"),
    (lambda: api_err(anthropic.NotFoundError, 404, "model not found"), "fatal"),
    (lambda: api_err(anthropic.BadRequestError, 400, "bad schema"), "invalid_input"),
    (lambda: api_err(anthropic.BadRequestError, 400,
                     "Your credit balance is too low to access the Anthropic API"), "fatal"),
    (lambda: api_err(anthropic.InternalServerError, 500), "transient"),
    (lambda: api_err(anthropic.APIStatusError, 529, "overloaded"), "transient"),
    (lambda: anthropic.APIConnectionError(request=httpx.Request("POST", "https://a")), "transient"),
    (lambda: anthropic.APITimeoutError(request=httpx.Request("POST", "https://a")), "transient"),
])
async def test_llm_error_mapping(exc, kind):
    with pytest.raises(ToolError) as e:
        await fake_llm(exc()).structured(system="s", prompt="p", output=_Out)
    assert e.value.kind == kind


async def test_llm_rate_limit_retry_after():
    with pytest.raises(ToolError) as e:
        await fake_llm(api_err(anthropic.RateLimitError, 429, headers={"retry-after": "7"})).structured(
            system="s", prompt="p", output=_Out)
    assert e.value.kind == "rate_limited" and e.value.retry_after_s == 7.0


async def test_llm_rate_limit_default_retry_after():
    with pytest.raises(ToolError) as e:
        await fake_llm(api_err(anthropic.RateLimitError, 429)).structured(system="s", prompt="p", output=_Out)
    assert e.value.retry_after_s == 5.0


@pytest.mark.parametrize("stop,parsed,kind", [
    ("refusal", None, "fatal"), ("max_tokens", "partial", "transient"), ("end_turn", None, "transient"),
])
async def test_llm_bad_stop_reasons(stop, parsed, kind):
    with pytest.raises(ToolError) as e:
        await fake_llm(types.SimpleNamespace(stop_reason=stop, parsed_output=parsed)).structured(
            system="s", prompt="p", output=_Out)
    assert e.value.kind == kind


async def test_llm_backed_tools_use_structured_and_recompute():
    ev = Evaluation(score=1, recommendation="reject", summary="s", criteria=[
        CriterionScore(name="a", weight=0.5, score=90, evidence=["x"]),
        CriterionScore(name="b", weight=0.5, score=70, evidence=["y"])])
    from api.tools.evaluator import Evaluator
    out = await Evaluator(fake_llm(types.SimpleNamespace(stop_reason="end_turn", parsed_output=ev))).evaluate(
        {"title": "x"}, {}, threshold=80)
    assert out.score == 80 and out.recommendation == "shortlist"

    r = Rubric(interview_themes=["t"], criteria=[RubricCriterion(name="a", weight=3, description="d"),
                                                 RubricCriterion(name="b", weight=1, description="d")])
    out = await RubricGenerator(fake_llm(types.SimpleNamespace(stop_reason="end_turn", parsed_output=r))).generate(
        {"title": "x"})
    assert [c.weight for c in out.criteria] == [0.75, 0.25]


async def test_llm_persona_returns_body_text():
    from api.tools.persona import _Body
    p = LLMCandidatePersona(fake_llm(types.SimpleNamespace(stop_reason="end_turn", parsed_output=_Body(body="hey"))))
    prof = await MockPloidClient().enrich("pl_001")
    assert await p.reply(prof, [Message(direction="out", body="hi")], "asks_question") == "hey"


def test_to_jsonable_handles_objects_and_models():
    class Obj:
        def __init__(self):
            self.a, self._hidden, self.items = 1, 2, (RoleForm(title="t"),)

    out = to_jsonable({"o": Obj(), "t": datetime})
    assert out["o"] == {"a": 1, "items": [RoleForm(title="t").model_dump(mode="json")]}
    assert out["t"] is datetime


# ======================= Evaluator _recompute =======================

def ev_of(*scores, weights=None) -> Evaluation:
    weights = weights or [1.0] * len(scores)
    return Evaluation(score=0, recommendation="reject", summary="s", criteria=[
        CriterionScore(name=f"c{i}", weight=w, score=s, evidence=["e"]) for i, (s, w) in enumerate(zip(scores, weights))])


@pytest.mark.parametrize("score,rec", [
    (100, "shortlist"), (75, "shortlist"), (74, "borderline"), (65, "borderline"), (64, "reject"), (0, "reject"),
])
def test_recompute_thresholds_default_75(score, rec):
    out = _recompute(ev_of(score), 75)
    assert out.score == score and out.recommendation == rec


@pytest.mark.parametrize("threshold,score,rec", [(80, 70, "borderline"), (80, 69, "reject"), (80, 80, "shortlist"),
                                                 (11, 0, "reject"), (10, 0, "borderline"), (0, 0, "shortlist")])
def test_recompute_custom_thresholds(threshold, score, rec):
    assert _recompute(ev_of(score), threshold).recommendation == rec


def test_recompute_weighted_average_overrides_model():
    out = _recompute(ev_of(100, 50, weights=[3, 1]), 75)
    assert out.score == 88 and out.recommendation == "shortlist"


def test_recompute_zero_weights_and_empty():
    assert _recompute(ev_of(90, 90, weights=[0, 0]), 75).score == 0
    assert _recompute(ev_of(), 75).score == 0


def test_recompute_does_not_mutate_input():
    ev = ev_of(90)
    _recompute(ev, 75)
    assert ev.score == 0 and ev.recommendation == "reject"


def test_recompute_half_rounds_up_at_threshold():
    assert _recompute(ev_of(74, 75), 75).recommendation == "shortlist"


# ======================= Rubric normalize / stub =======================

def rub(*ws) -> Rubric:
    return Rubric(interview_themes=["t"],
                  criteria=[RubricCriterion(name=f"c{i}", weight=w, description="d") for i, w in enumerate(ws)])


def test_normalize_scales_to_one():
    assert [c.weight for c in normalize(rub(2, 1, 1)).criteria] == [0.5, 0.25, 0.25]
    assert abs(sum(c.weight for c in normalize(rub(1, 1, 1)).criteria) - 1) < 0.01
    assert abs(sum(c.weight for c in normalize(rub(*[1] * 7)).criteria) - 1) < 0.01


def test_normalize_already_normalized_and_rounding():
    assert [c.weight for c in normalize(rub(0.6, 0.4)).criteria] == [0.6, 0.4]
    assert normalize(rub(1, 2)).criteria[0].weight == 0.333


def test_normalize_preserves_names_and_themes():
    n = normalize(rub(5, 5))
    assert [c.name for c in n.criteria] == ["c0", "c1"] and n.interview_themes == ["t"]


def test_normalize_all_zero_weights():
    assert abs(sum(c.weight for c in normalize(rub(0, 0, 0)).criteria) - 1) < 0.01


async def test_stub_rubric_min_four_criteria_for_sparse_form():
    r = await RubricGenerator(None).generate(RoleForm(title="Founding Engineer"))
    assert len(r.criteria) >= 4


async def test_stub_rubric_accepts_dict_and_caps_at_five():
    r = await RubricGenerator(None).generate({"title": "x", "must_haves": list("ABCDEF"),
                                              "nice_to_haves": list("GHI")})
    assert len(r.criteria) == 5 and [c.name for c in r.criteria][:4] == list("ABCD")
    assert r.criteria[0].weight > r.criteria[4].weight


# ======================= Persona =======================

async def test_persona_no_reply_raises():
    prof = await MockPloidClient().enrich("pl_001")
    with pytest.raises(ValueError):
        await LLMCandidatePersona(None).reply(prof, [], "no_reply")


async def test_persona_no_reply_raises_before_llm_call():
    llm = fake_llm(RuntimeError("must not be called"))
    prof = await MockPloidClient().enrich("pl_001")
    with pytest.raises(ValueError):
        await LLMCandidatePersona(llm).reply(prof, [], "no_reply")
    assert llm._client.messages.calls == []


@pytest.mark.parametrize("disp,intent", [("interested", "interested"), ("not_interested", "declined"),
                                         ("asks_question", "question")])
async def test_persona_stub_replies_classify_consistently(disp, intent):
    t = build_tools(S())
    prof = await t.ploid.enrich("pl_002")
    txt = await t.persona.reply(prof, [Message(direction="out", body="hi")], disp)
    assert (await t.classifier.classify(txt)).intent == intent


# ======================= Mock ElevenLabs delay =======================

async def test_mock_elevenlabs_delay(monkeypatch):
    import api.tools.elevenlabs as el
    now = [1000.0]
    monkeypatch.setattr(el.time, "monotonic", lambda: now[0])
    c = MockElevenLabsClient("http://h/", delay_s=15.0)
    s = await c.create_interview("cand", ["q"], "ctx")
    assert s.join_url == f"http://h/api/webhooks/mock-interview/{s.conversation_id}"
    assert await c.get_transcript(s.conversation_id) is None
    now[0] += 14.9
    assert await c.get_transcript(s.conversation_id) is None
    now[0] += 0.2
    t = await c.get_transcript(s.conversation_id)
    assert t is not None and t.conversation_id == s.conversation_id and t.duration_s > 0
    assert await c.find_conversation("cand", datetime.now(timezone.utc)) == s.conversation_id
    assert await c.find_conversation("other", datetime.now(timezone.utc)) is None


async def test_mock_elevenlabs_unknown_id_is_done_immediately():
    c = MockElevenLabsClient("http://h", delay_s=999)
    assert (await c.get_transcript("mock_conv_after_restart")) is not None


async def test_mock_elevenlabs_requires_questions_and_unique_ids():
    c = MockElevenLabsClient("http://h", delay_s=0)
    with pytest.raises(ToolError) as e:
        await c.create_interview("c", [], "x")
    assert e.value.kind == "invalid_input"
    a = await c.create_interview("c", ["q"], "x")
    b = await c.create_interview("c", ["q"], "x")
    assert a.conversation_id != b.conversation_id
    assert await c.find_conversation("c", datetime.now(timezone.utc)) == b.conversation_id


def test_transcript_fixture_valid():
    d = json.loads((ROOT / "fixtures/transcript.json").read_text())
    turns = [Turn(**t) for t in d["turns"]]
    assert {t.role for t in turns} == {"agent", "candidate"} and d["duration_s"] > 0


# ======================= poll_interviews resilience =======================

def _tx(cid="conv") -> InterviewTranscript:
    return InterviewTranscript(conversation_id=cid, turns=[], duration_s=1, ended_at=datetime.now(timezone.utc))


class _Client:
    def __init__(self, transcripts: dict, raises: dict | None = None):
        self.transcripts, self.raises = transcripts, raises or {}

    async def find_conversation(self, cand, since):
        return f"conv_{cand}"

    async def get_transcript(self, conv):
        if conv in self.raises:
            raise self.raises[conv]
        return self.transcripts.get(conv)


async def _run_poller(client, get_pending, on_done, until, timeout=2.0):
    stop = asyncio.Event()
    task = asyncio.create_task(poll_interviews(client, get_pending, on_done, interval_s=0.01, stop=stop))
    try:
        for _ in range(int(timeout / 0.01)):
            if until():
                break
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await asyncio.wait_for(task, 1)


def P(cid, conv=None):
    return PendingInterview(cid, conv, datetime.now(timezone.utc))


async def test_poller_survives_get_pending_raising():
    calls = {"n": 0}
    done = []

    def get_pending():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("db locked")
        return [P("a")]

    await _run_poller(_Client({"conv_a": _tx()}), get_pending, lambda c, t: done.append(c), lambda: done)
    assert done == ["a"] and calls["n"] >= 3


async def test_poller_survives_toolerror_and_async_callbacks():
    calls = {"n": 0}
    done = []

    async def get_pending():
        return [P("a", conv="conv_a")]

    class C(_Client):
        async def get_transcript(self, conv):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ToolError("transient", "blip")
            return _tx()

    async def on_done(c, t):
        done.append(c)

    await _run_poller(C({}), get_pending, on_done, lambda: done)
    assert done == ["a"]


async def test_poller_skips_unstarted_and_stops_promptly():
    done = []
    await _run_poller(_Client({}), lambda: [P("a")], lambda c, t: done.append(c), lambda: False, timeout=0.1)
    assert done == []


async def test_poller_retries_when_on_done_fails():
    attempts = []

    def on_done(c, t):
        attempts.append(c)
        if len(attempts) == 1:
            raise RuntimeError("db busy")

    await _run_poller(_Client({"conv_a": _tx()}), lambda: [P("a")], on_done, lambda: len(attempts) >= 2,
                      timeout=0.5)
    assert len(attempts) >= 2


async def test_poller_one_bad_candidate_does_not_block_others():
    done = []
    client = _Client({"conv_b": _tx()}, raises={"conv_a": ToolError("fatal", "conversation failed")})
    await _run_poller(client, lambda: [P("a"), P("b")], lambda c, t: done.append(c), lambda: done, timeout=0.3)
    assert done == ["b"]


# ======================= Webhook route =======================

WH_SECRET = "secret"


def _real_el() -> RealElevenLabsClient:
    return RealElevenLabsClient("xi", "ag", WH_SECRET)


def _sig(body: bytes) -> dict:
    import hashlib, hmac, time as _t
    ts = str(int(_t.time()))
    mac = hmac.new(WH_SECRET.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"ElevenLabs-Signature": f"t={ts},v0={mac}"}


def _app(monkeypatch, elevenlabs):
    from collections import OrderedDict
    from api.routes import webhooks
    monkeypatch.setattr(webhooks, "_seen_conversations", OrderedDict())
    tools = build_tools(S())
    tools.elevenlabs = elevenlabs
    monkeypatch.setattr(webhooks, "get_tools", lambda: tools)
    app = FastAPI()
    app.include_router(webhooks.router)
    return TestClient(app, raise_server_exceptions=False)


def _body(cand="cand_1", job="job_1", typ="post_call_transcription") -> bytes:
    import time as _t
    exp, tok = _real_el().signer.sign(cand, job)
    dv = {"candidate_id": cand, "job_id": job, "interview_expires": str(exp), "interview_token": tok}
    return json.dumps({"type": typ, "data": {
        "conversation_id": "conv_1", "transcript": [{"role": "agent", "message": "hi"}],
        "metadata": {"start_time_unix_secs": int(_t.time()) - 60, "call_duration_secs": 5},
        "conversation_initiation_client_data": {"dynamic_variables": dv}}}).encode()


def test_webhook_503_when_repo_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "api.db", None)  # makes `from api.db import repo` raise ImportError
    monkeypatch.delitem(sys.modules, "api.db.repo", raising=False)
    client = _app(monkeypatch, _real_el())
    b = _body()
    r = client.post("/api/webhooks/elevenlabs", content=b, headers=_sig(b))
    assert r.status_code == 503 and "api.db.repo" in r.json()["detail"]


def test_webhook_awaits_async_repo(monkeypatch):
    emitted = []
    repo = types.ModuleType("api.db.repo")

    async def emit_event(type, **kw):
        emitted.append((type, kw))

    repo.emit_event = emit_event
    db = types.ModuleType("api.db")
    db.repo = repo
    monkeypatch.setitem(sys.modules, "api.db", db)
    monkeypatch.setitem(sys.modules, "api.db.repo", repo)
    client = _app(monkeypatch, _real_el())
    b = _body()
    r = client.post("/api/webhooks/elevenlabs", content=b, headers=_sig(b))
    assert r.status_code == 200 and r.json() == {"ok": True}
    (etype, kw), = emitted
    assert etype == "InterviewDone" and kw["payload"]["source"] == "webhook"


def test_webhook_ignores_other_types_without_repo(monkeypatch):
    monkeypatch.setitem(sys.modules, "api.db", None)
    client = _app(monkeypatch, _real_el())
    b = _body(typ="post_call_audio")
    r = client.post("/api/webhooks/elevenlabs", content=b, headers=_sig(b))
    assert r.status_code == 200 and r.json()["ignored"] is True


def test_webhook_missing_job_id_only_is_ignored(monkeypatch):
    monkeypatch.setitem(sys.modules, "api.db", None)
    client = _app(monkeypatch, _real_el())
    b = _body(job="")
    r = client.post("/api/webhooks/elevenlabs", content=b, headers=_sig(b))
    assert r.status_code == 200 and r.json()["ignored"] is True


def test_webhook_mock_client_rejects_everything(monkeypatch):
    client = _app(monkeypatch, MockElevenLabsClient("http://h", 0))
    b = _body()
    assert client.post("/api/webhooks/elevenlabs", content=b, headers=_sig(b)).status_code == 401


def test_webhook_real_client_rejects_unsigned(monkeypatch):
    client = _app(monkeypatch, RealElevenLabsClient("xi", "ag", "secret"))
    assert client.post("/api/webhooks/elevenlabs", content=_body()).status_code == 401


def test_webhook_real_client_without_secret_rejects_everything(monkeypatch):
    client = _app(monkeypatch, RealElevenLabsClient("xi", "ag", None))
    r = client.post("/api/webhooks/elevenlabs", content=_body(), headers={"ElevenLabs-Signature": "t=1,v0=a"})
    assert r.status_code == 401


def test_mock_interview_page_escapes_id(monkeypatch):
    client = _app(monkeypatch, MockElevenLabsClient("http://h", 0))
    r = client.get("/api/webhooks/mock-interview/<script>x")
    assert r.status_code == 200 and "<script>" not in r.text and "&lt;script&gt;" in r.text


# ======================= Fixture integrity =======================

def _rows():
    return json.loads((ROOT / "fixtures/ploid_candidates.json").read_text())


def _norm(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())


def test_fixture_has_8_unique_candidates():
    rows = _rows()
    assert len(rows) == 8
    assert len({r["ploid_id"] for r in rows}) == 8


def test_fixture_rows_validate():
    for r in _rows():
        p = EnrichedProfile(**r["profile"])
        assert p.ploid_id == r["ploid_id"] and _norm(p.name) == _norm(r["name"])
        assert 0 <= p.completeness <= 1


def test_fixture_exactly_one_duplicate_pair_sharing_url():
    rows = _rows()
    pairs = [(a["ploid_id"], b["ploid_id"]) for i, a in enumerate(rows) for b in rows[i + 1:]
             if _norm(a["name"]) == _norm(b["name"])]
    assert pairs == [("pl_001", "pl_006")]
    a, b = (next(r for r in rows if r["ploid_id"] == pid) for pid in pairs[0])
    assert set(a["profile_urls"]) & set(b["profile_urls"])
    url_owners = {}
    for r in rows:
        for u in r["profile_urls"]:
            url_owners.setdefault(u, set()).add(r["ploid_id"])
    assert [o for o in url_owners.values() if len(o) > 1] == [{"pl_001", "pl_006"}]


def test_fixture_duplicate_within_first_five():
    assert "pl_006" in [r["ploid_id"] for r in _rows()[:5]]


def test_fixture_has_thin_non_duplicate_profile():
    thin = [r["ploid_id"] for r in _rows() if r["profile"]["completeness"] < 0.6]
    assert "pl_005" in thin  # the non-duplicate thin profile
    assert set(thin) <= {"pl_005", "pl_006"}  # pl_006 (duplicate) is also thin


def test_fixture_pl_008_has_no_contact_and_others_do():
    contacts = json.loads((ROOT / "fixtures/fiber_contacts.json").read_text())
    ids = {r["ploid_id"] for r in _rows()}
    assert "pl_008" not in contacts and set(contacts) == ids - {"pl_008"}
    for c in contacts.values():
        ContactInfo(**c)


async def test_mock_fiber_pl_008_not_found_even_by_name():
    with pytest.raises(ToolError) as e:
        await MockFiberClient().find_contact("Aisha Mohammed", {}, None)
    assert e.value.kind == "not_found"
