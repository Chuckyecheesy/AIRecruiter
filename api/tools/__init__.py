"""Lane B -> lane A/C handoff: `get_tools()` returns every adapter, mock or real per config.

    from api.tools import get_tools, tools_status
    tools = get_tools()
    profile = await tools.ploid.enrich("pl_001")
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from datetime import timedelta
from functools import lru_cache
from typing import Any

from api.config import Settings, get_settings
from api.tools.base import (
    BrowserTool, CandidatePersona, ElevenLabsClient, EmailSender, FailingTool, FiberClient,
    PloidClient, ToolError,
)
from api.tools.browser import DisabledBrowserTool
from api.tools.drafter import Drafter
from api.tools.elevenlabs import MockElevenLabsClient, RealElevenLabsClient
from api.tools.email_sim import EmailSimulator
from api.tools.evaluator import Evaluator
from api.tools.fiber import DemoInboxFiber, MockFiberClient, RealFiberClient
from api.tools.llm import LLM
from api.tools.persona import LLMCandidatePersona, ReplyClassifier, pick_disposition
from api.tools.ploid import MockPloidClient, RealPloidClient
from api.tools.rubric import RubricGenerator

log = logging.getLogger(__name__)

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
        if not s.llm_enabled:
            return None
        return LLM(s.anthropic_api_key, model, timeout_s=s.llm_timeout_s, provider=s.llm_provider, settings=s)

    def fallback(tool: str, missing: list[str]) -> str:
        """A tool set to `real` without credentials falls back to mock, loudly."""
        if s.mode_for(tool) != "real":
            return "mock"
        log.warning("%s: mode is real but %s not set; using MOCK", tool, " / ".join(missing))
        return f"mock (real requested; missing {', '.join(missing)})"

    llm_status = (f"real ({s.llm_provider}: {s.gemini_model})" if s.llm_provider == "gemini" else "real") \
        if s.llm_enabled else "stub"

    # Ploid
    if s.mode_for("ploid") == "real" and s.ploid_api_key and s.ploid_base_url:
        ploid: Any = RealPloidClient(s.ploid_api_key, s.ploid_base_url); status["ploid"] = "real"
    else:
        ploid = MockPloidClient()
        status["ploid"] = fallback("ploid", [k for k in ("PLOID_API_KEY", "PLOID_BASE_URL")
                                             if not getattr(s, k.lower())])

    # Fiber (always wrapped by the demo-inbox rewrite)
    if s.mode_for("fiber") == "real" and s.fiber_api_key and s.fiber_base_url:
        fiber_inner: Any = RealFiberClient(s.fiber_api_key, s.fiber_base_url); status["fiber"] = "real"
    else:
        fiber_inner = MockFiberClient()
        status["fiber"] = fallback("fiber", [k for k in ("FIBER_API_KEY", "FIBER_BASE_URL")
                                             if not getattr(s, k.lower())])
    fiber: Any = DemoInboxFiber(fiber_inner, s.demo_inbox)

    # Browser (stretch)
    browser: Any = DisabledBrowserTool(); status["browser"] = "off"

    # ElevenLabs
    if s.mode_for("elevenlabs") == "real" and s.elevenlabs_api_key and s.elevenlabs_agent_id:
        elevenlabs: Any = RealElevenLabsClient(
            s.elevenlabs_api_key, s.elevenlabs_agent_id, s.elevenlabs_webhook_secret, s.elevenlabs_base_url,
            token_secret=s.interview_token_secret, ttl=timedelta(hours=s.interview_ttl_hours))
        status["elevenlabs"] = "real" + (" (poll)" if s.elevenlabs_poll else " (webhook)")
        if not s.elevenlabs_poll and not s.elevenlabs_webhook_secret:
            log.warning("elevenlabs: no ELEVENLABS_WEBHOOK_SECRET; every webhook will be rejected (401)")
            status["elevenlabs"] += " (no webhook secret!)"
    else:
        elevenlabs = MockElevenLabsClient(s.public_base_url, s.mock_interview_delay_s)
        status["elevenlabs"] = fallback("elevenlabs", [k for k in ("ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID")
                                                       if not getattr(s, k.lower())])

    email: Any = EmailSimulator(s.allow_real_email, s.email_allowlist_set); status["email"] = "simulated"

    tools = Tools(
        ploid=ploid, fiber=fiber, browser=browser, email=email,
        persona=LLMCandidatePersona(llm(s.model_draft)), elevenlabs=elevenlabs,
        drafter=Drafter(llm(s.model_draft), company_name=s.company_name, ceo_name=s.ceo_name), evaluator=Evaluator(llm(s.model_eval)),
        rubric=RubricGenerator(llm(s.model_draft)), classifier=ReplyClassifier(llm(s.model_draft)),
        status=status,
    )
    status.update({"drafter": llm_status, "evaluator": llm_status, "rubric": llm_status, "persona": llm_status})

    if s.fail_tool:
        name = s.fail_tool.strip().lower()
        valid = [f.name for f in fields(Tools) if f.name != "status"]
        if name not in valid:
            raise ValueError(f"FAIL_TOOL={s.fail_tool!r} is not a tool name (one of: {', '.join(valid)})")
        setattr(tools, name, FailingTool(getattr(tools, name), name))
        status[name] = f"{status.get(name, '?')} (FAIL_TOOL)"
    return tools


@lru_cache
def get_tools() -> Tools:
    return build_tools(get_settings())


def tools_status() -> dict[str, str]:
    """For lane C's GET /health."""
    return dict(get_tools().status)
