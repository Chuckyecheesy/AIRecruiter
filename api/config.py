"""All env-driven settings (lane B). Everyone imports `settings` from here."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["mock", "real", "off"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    # Claude
    anthropic_api_key: str | None = None
    model_decide: str = "claude-sonnet-5-5"
    model_draft: str = "claude-sonnet-5-5"
    model_eval: str = "claude-sonnet-5-5"
    llm_mode: Literal["real", "stub"] = "real"
    llm_provider: Literal["anthropic", "gemini"] = "anthropic"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.8-flash"  # used for every role when LLM_PROVIDER=gemini
    llm_timeout_s: float = 45.0
    # Used in outreach sign-offs; never invented by the model.
    company_name: str | None = None
    ceo_name: str | None = None

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
    elevenlabs_base_url: str = "https://api.elevenlabs.io"
    mock_interview_delay_s: float = 15.0
    # Signs the per-interview token in the join link (falls back to the webhook secret / API key).
    interview_token_secret: str | None = None
    interview_ttl_hours: float = 2.0
    webhook_max_bytes: int = 2_000_000

    # Langfuse
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # Email safety
    demo_inbox: str = "demo@example.com"
    allow_real_email: bool = False
    email_allowlist: str = ""  # comma-separated
    smtp_host: str | None = None       # e.g. smtp.gmail.com
    smtp_port: int = 587
    smtp_user: str | None = None       # e.g. you@gmail.com
    smtp_password: str | None = None   # Gmail: an App Password, not your login password
    smtp_from: str | None = None

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

    @field_validator("demo_inbox")
    @classmethod
    def _check_demo_inbox(cls, v: str) -> str:
        local, at, domain = v.strip().partition("@")
        if not (local and at and "." in domain):
            raise ValueError(f"DEMO_INBOX must be an email address, got {v!r}")
        return v.strip()

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
