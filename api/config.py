"""All env-driven settings (lane B). Everyone imports `settings` from here."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["mock", "real", "off"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LLM provider
    llm_provider: Literal["anthropic", "gemini"] = "gemini"
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None
    model_decide: str = "gemini-2.5-flash"
    model_draft: str = "gemini-2.5-flash"
    model_eval: str = "gemini-2.5-flash"
    llm_mode: Literal["real", "stub"] = "real"

    # Tool modes (per-tool override wins over TOOLS_MODE)
    tools_mode: Literal["mock", "real"] = "mock"
    ploid_mode: Mode | None = None
    fiber_mode: Mode | None = None
    fibre_mode: Mode | None = None
    elevenlabs_mode: Mode | None = None
    browser_mode: Mode | None = None
    fail_tool: str | None = None  # demo flag: e.g. "fiber" -> that tool always raises transient

    # Ploid / Fiber
    ploid_api_key: str | None = None
    ploid_base_url: str | None = None
    fiber_api_key: str | None = None
    fiber_base_url: str | None = None
    fibre_api_key: str | None = None
    fibre_base_url: str | None = None

    # ElevenLabs
    elevenlabs_api_key: str | None = None
    elevenlabs_agent_id: str | None = None
    elevenlabs_webhook_secret: str | None = None
    elevenlabs_poll: bool = False
    elevenlabs_base_url: str = "https://api.elevenlabs.io"
    mock_interview_delay_s: float = 15.0

    # Langfuse
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # Email safety
    demo_inbox: str = "demo@example.com"
    allow_real_email: bool = False
    email_allowlist: str = ""  # comma-separated

    # Runtime
    persona_delay_s: float = 15.0
    followup_after_hours: float = 24.0  # one nudge after this long without a reply
    stale_after_hours: float = 48.0     # then mark_stale after this long without a reply to the nudge
    tick_interval_s: float = 15.0
    max_concurrent_runs: int = 4
    job_id: str | None = None
    admin_enabled: bool = False
    data_dir: str = "./data"
    public_base_url: str = "http://localhost:8000"
    demo_token: str | None = None

    def mode_for(self, tool: str) -> Mode:
        """Resolve mock/real/off for a tool ("ploid", "fiber", "elevenlabs", "browser")."""
        if tool == "fibre":
            tool = "fiber"
        override = getattr(self, f"{tool}_mode", None)
        if override:
            return override
        if tool == "browser":
            return "off"  # browser is a stretch goal; opt in with BROWSER_MODE=real
        return self.tools_mode

    @property
    def email_allowlist_set(self) -> set[str]:
        return {e.strip().lower() for e in self.email_allowlist.split(",") if e.strip()}

    @property
    def llm_enabled(self) -> bool:
        # Set LLM_MODE=stub for fully offline work.
        return self.llm_mode == "real"

    @property
    def llm_api_key(self) -> str | None:
        return self.gemini_api_key if self.llm_provider == "gemini" else self.anthropic_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
