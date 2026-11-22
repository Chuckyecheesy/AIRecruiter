"""Lane B -> lane A/C handoff: `get_tools()` returns every adapter, mock or real per config.

    from api.tools import get_tools, tools_status
    tools = get_tools()
    profile = await tools.ploid.enrich("pl_001")
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from api.config import Settings, get_settings
from api.tools.base import (
    BrowserTool, CandidatePersona, ElevenLabsClient, EmailSender, FailingTool, FiberClient,
    PloidClient, ToolError,
)
from api.tools.browser import DisabledBrowserTool
from api.tools.drafter import Drafter
from api.tools.elevenlabs import MockElevenLabsClient
from api.tools.email_sim import EmailSimulator
from api.tools.evaluator import Evaluator
from api.tools.fiber import DemoInboxFiber, MockFiberClient, RealFiberClient
from api.tools.llm import LLM
from api.tools.persona import LLMCandidatePersona, ReplyClassifier, pick_disposition
from api.tools.ploid import MockPloidClient, RealPloidClient
from api.tools.rubric import RubricGenerator

__all__ = ["Tools", "get_tools", "tools_status", "ToolError", "pick_disposition"]


@dataclass
class Tools:
    ploid: PloidClient
    fiber: FiberClient
    browser: BrowserTool
    email: EmailSender
    persona: CandidatePersona
    elevenlabs: ElevenLabsClient
    drafter: Drafter
    evaluator: Evaluator
    rubric: RubricGenerator
    classifier: ReplyClassifier
    status: dict[str, str]


def build_tools(s: Settings) -> Tools:
    status: dict[str, str] = {}

    def llm(model: str) -> LLM | None:
        return LLM(s.anthropic_api_key, model) if s.llm_enabled else None

    llm_status = "real" if s.llm_enabled else "stub"

    # Ploid
    if s.mode_for("ploid") == "real" and s.ploid_api_key and s.ploid_base_url:
        ploid: Any = RealPloidClient(s.ploid_api_key, s.ploid_base_url); status["ploid"] = "real"
    else:
        ploid = MockPloidClient(); status["ploid"] = "mock"

    # Fiber (always wrapped by the demo-inbox rewrite)
    if s.mode_for("fiber") == "real" and s.fiber_api_key and s.fiber_base_url:
        fiber_inner: Any = RealFiberClient(s.fiber_api_key, s.fiber_base_url); status["fiber"] = "real"
    else:
        fiber_inner = MockFiberClient(); status["fiber"] = "mock"
    fiber: Any = DemoInboxFiber(fiber_inner, s.demo_inbox)

    # Browser (stretch)
    browser: Any = DisabledBrowserTool(); status["browser"] = "off"

    # ElevenLabs (real client lands in the 1:40-2:50 block)
    elevenlabs: Any = MockElevenLabsClient(s.public_base_url, s.mock_interview_delay_s)
    status["elevenlabs"] = "mock"

    email: Any = EmailSimulator(s.allow_real_email, s.email_allowlist_set); status["email"] = "simulated"

    tools = Tools(
        ploid=ploid, fiber=fiber, browser=browser, email=email,
        persona=LLMCandidatePersona(llm(s.model_draft)), elevenlabs=elevenlabs,
        drafter=Drafter(llm(s.model_draft)), evaluator=Evaluator(llm(s.model_eval)),
        rubric=RubricGenerator(llm(s.model_draft)), classifier=ReplyClassifier(llm(s.model_draft)),
        status=status,
    )
    status.update({"drafter": llm_status, "evaluator": llm_status, "rubric": llm_status, "persona": llm_status})

    if s.fail_tool:
        name = s.fail_tool.strip().lower()
        if not hasattr(tools, name):
            raise ValueError(f"FAIL_TOOL={s.fail_tool!r} is not a tool name")
        setattr(tools, name, FailingTool(getattr(tools, name), name))
        status[name] = f"{status.get(name, '?')} (FAIL_TOOL)"
    return tools


@lru_cache
def get_tools() -> Tools:
    return build_tools(get_settings())


def tools_status() -> dict[str, str]:
    """For lane C's GET /health."""
    return dict(get_tools().status)
