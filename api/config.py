"""All env-driven settings (lane B). Everyone imports `settings` from here."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["mock", "real", "off"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Claude
    anthropic_api_key: str | None = None
    model_decide: str = "claude-sonnet-5-5"
    model_draft: str = "claude-sonnet-5-5"
    model_eval: str = "claude-sonnet-5-5"
    llm_mode: Literal["real", "stub"] = "real"

    # Tool modes (per-tool override wins over TOOLS_MODE)
    tools_mode: Literal["mock", "real"] = "mock"
    ploid_mode: Mode | None = None
    fiber_mode: Mode | None = None
    elevenlabs_mode: Mode | None = None
    browser_mode: Mode | None = None
    fail_tool: str | None = None  # demo flag: e.g. "fiber" -> that tool always raises transient

    # Ploid / Fiber
    ploid_api_key: str | None = None
    ploid_base_url: str | None = None
    fiber_api_key: str | None = None
    fiber_base_url: str | None = None

    # ElevenLabs
    elevenlabs_api_key: str | None = None
    elevenlabs_agent_id: str | None = None
    elevenlabs_webhook_secret: str | None = None
    elevenlabs_poll: bool = False
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
    max_concurrent_runs: int = 4
    job_id: str | None = None
    admin_enabled: bool = False
    data_dir: str = "./data"
    public_base_url: str = "http://localhost:8000"
    demo_token: str | None = None

    def mode_for(self, tool: str) -> Mode:
        """Resolve mock/real/off for a tool ("ploid", "fiber", "elevenlabs", "browser")."""
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
        # The SDK also resolves ANTHROPIC_AUTH_TOKEN / `ant auth login` profiles, so don't require the key.
        # Set LLM_MODE=stub for fully offline work.
        return self.llm_mode == "real"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
