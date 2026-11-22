import random
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from api.config import Settings
from api.tools.llm import LLM
from api.tools import ToolError, build_tools, pick_disposition
from api.tools.fiber import demo_address
from api.tools.models import EnrichedProfile, Message, RoleForm, SearchCriteria


def mock_settings(**kw) -> Settings:
    base = dict(tools_mode="mock", llm_mode="stub", anthropic_api_key=None, demo_inbox="demo@example.com",
                mock_interview_delay_s=0.0, _env_file=None)
    return Settings(**{**base, **kw})


def test_gemini_provider_is_default():
    s = Settings(_env_file=None)
    assert s.llm_provider == "gemini"
    assert s.llm_api_key == s.gemini_api_key


async def test_gemini_structured_output(monkeypatch):
    class Output(BaseModel):
        value: str

    class Models:
        async def generate_content(self, **kwargs):
            assert kwargs["config"].response_schema is Output
            return SimpleNamespace(parsed={"value": "ok"}, text='{"value":"ok"}')

    client = SimpleNamespace(aio=SimpleNamespace(models=Models()))
    monkeypatch.setattr("api.tools.llm.genai.Client", lambda **kwargs: client)

    result = await LLM("test-key", "gemini-2.5-flash", provider="gemini").structured(
        system="system", prompt="prompt", output=Output
    )

    assert result == Output(value="ok")


@pytest.fixture
def tools():
    return build_tools(mock_settings())


async def test_status_all_mock(tools):
    assert tools.status["ploid"] == "mock"
    assert tools.status["fiber"] == "mock"
    assert tools.status["email"] == "simulated"
    assert tools.status["drafter"] == "stub"


async def test_ploid_search_includes_duplicate_and_thin(tools):
    res = await tools.ploid.search(SearchCriteria(title="AI Engineer"), limit=8)
    assert len(res) == 8
    ids = [c.ploid_id for c in res[:5]]
    assert "pl_006" in ids  # duplicate is always within the first max_candidates+1
    profiles = [await tools.ploid.enrich(c.ploid_id) for c in res]
    assert any(p.completeness < 0.6 for p in profiles)


async def test_ploid_enrich_not_found(tools):
    with pytest.raises(ToolError) as e:
        await tools.ploid.enrich("nope")
    assert e.value.kind == "not_found"


async def test_fiber_rewrites_to_demo_inbox(tools):
    p = await tools.ploid.enrich("pl_001")
    c = await tools.fiber.find_contact(p.name, p.links, None)
    assert c.email == "demo+sarah-chen@example.com"
    assert c.phone is None


async def test_fiber_not_found(tools):
    p = await tools.ploid.enrich("pl_008")
    with pytest.raises(ToolError) as e:
        await tools.fiber.find_contact(p.name, p.links, None)
    assert e.value.kind == "not_found"


def test_demo_address():
    assert demo_address("inbox@x.io", "01JABC") == "inbox+01jabc@x.io"


async def test_email_is_simulated(tools):
    r = await tools.email.send("demo+a@example.com", "hi", "body", "thread_1")
    assert r.simulated and r.message_id.startswith("sim_")


async def test_email_guard_blocks_real_send():
    t = build_tools(mock_settings(allow_real_email=True, email_allowlist="me@x.io"))
    with pytest.raises(ToolError) as e:
        await t.email.send("me@x.io", "s", "b", "t")
    assert e.value.kind == "fatal"


async def test_persona_and_disposition(tools):
    p = await tools.ploid.enrich("pl_001")
    txt = await tools.persona.reply(p, [Message(direction="out", body="hi")], "interested")
    assert txt
    rng = random.Random(0)
    counts = {d: 0 for d in ("interested", "not_interested", "asks_question", "no_reply")}
    for _ in range(2000):
        counts[pick_disposition(rng)] += 1
    assert 0.55 < counts["interested"] / 2000 < 0.65


async def test_classifier_stub(tools):
    assert (await tools.classifier.classify("Is it remote?")).intent == "question"
    assert (await tools.classifier.classify("Not interested, thanks")).intent == "declined"


async def test_drafter_feedback_changes_draft(tools):
    job = {"title": "AI Engineer"}
    mem = {"profile": (await tools.ploid.enrich("pl_001")).model_dump()}
    a = await tools.drafter.outreach(job, mem)
    b = await tools.drafter.outreach(job, mem, feedback="shorter")
    assert a.body != b.body and "AI Study Assistant" in a.body


async def test_rubric_weights_sum_to_one(tools):
    r = await tools.rubric.generate(RoleForm(title="AI Engineer", must_haves=["RAG", "TypeScript"],
                                             nice_to_haves=["LangGraph"]))
    assert 4 <= len(r.criteria) <= 6
    assert abs(sum(c.weight for c in r.criteria) - 1) < 0.01


async def test_evaluator_stub_recomputes(tools):
    r = await tools.rubric.generate(RoleForm(title="AI Engineer", must_haves=["RAG", "TypeScript"]))
    ev = await tools.evaluator.evaluate({"title": "AI Engineer", "rubric": r.model_dump()}, {})
    assert ev.score == 85 and ev.recommendation == "shortlist"


async def test_elevenlabs_mock_flow(tools):
    s = await tools.elevenlabs.create_interview("c1", ["Tell me about RAG"], "ctx")
    t = await tools.elevenlabs.get_transcript(s.conversation_id)
    assert t is not None and len(t.turns) > 4


async def test_fail_tool_wrapper():
    t = build_tools(mock_settings(fail_tool="fiber"))
    with pytest.raises(ToolError) as e:
        await t.fiber.find_contact("x", {}, None)
    assert e.value.kind == "transient"
    assert "FAIL_TOOL" in t.status["fiber"]
