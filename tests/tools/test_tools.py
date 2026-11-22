import random

import pytest

from api.config import Settings
from api.tools import ToolError, build_tools, pick_disposition
from api.tools.fiber import demo_address
from api.tools.models import EnrichedProfile, Message, RoleForm, SearchCriteria


def mock_settings(**kw) -> Settings:
    base = dict(tools_mode="mock", llm_mode="stub", anthropic_api_key=None, demo_inbox="demo@example.com",
                mock_interview_delay_s=0.0, _env_file=None)
    return Settings(**{**base, **kw})


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


async def test_email_guard_needs_flag_allowlist_and_smtp():
    # flags set but no SMTP configured -> still simulated, never a real send
    t = build_tools(mock_settings(allow_real_email=True, email_allowlist="me@x.io"))
    assert (await t.email.send("me@x.io", "s", "b", "t")).simulated
    t = build_tools(mock_settings(allow_real_email=True, email_allowlist="me@x.io", smtp_host="h",
                                  smtp_user="u", smtp_password="p"))
    assert t.email.real_send_allowed("me+sarah@x.io") and not t.email.real_send_allowed("other@x.io")


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


async def test_evaluator_stub_separates_strong_and_weak(tools):
    r = await tools.rubric.generate(RoleForm(title="AI Engineer", must_haves=["RAG", "TypeScript"]))
    job = {"title": "AI Engineer", "rubric": r.model_dump()}
    strong = await tools.evaluator.evaluate(job, {"profile": (await tools.ploid.enrich("pl_001")).model_dump()})
    weak = await tools.evaluator.evaluate(job, {"profile": (await tools.ploid.enrich("pl_004")).model_dump()})
    assert strong.score > weak.score
    assert weak.recommendation == "reject"
    assert [c.name for c in strong.criteria] == [c.name for c in r.criteria]


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
