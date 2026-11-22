"""Tool protocols, error taxonomy, and the FAIL_TOOL demo wrapper (spec 02)."""
from __future__ import annotations

import functools
import inspect
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from api.tools.models import (
    BrowserFindings,
    ContactInfo,
    Disposition,
    EnrichedProfile,
    InterviewSession,
    InterviewTranscript,
    Message,
    PloidCandidate,
    SearchCriteria,
    SendReceipt,
)

ErrorKind = Literal["transient", "rate_limited", "not_found", "invalid_input", "fatal"]


class ToolError(Exception):
    """The only exception adapters raise. Lane A's `execute` maps `kind` to retry behavior."""

    def __init__(self, kind: ErrorKind, message: str = "", retry_after_s: float | None = None):
        super().__init__(message or kind)
        self.kind: ErrorKind = kind
        self.retry_after_s = retry_after_s

    def __repr__(self) -> str:
        return f"ToolError(kind={self.kind!r}, message={str(self)!r}, retry_after_s={self.retry_after_s})"


@runtime_checkable
class PloidClient(Protocol):
    async def search(self, criteria: SearchCriteria, limit: int) -> list[PloidCandidate]: ...
    async def enrich(self, ploid_id: str) -> EnrichedProfile: ...


@runtime_checkable
class FiberClient(Protocol):
    async def find_contact(
        self, name: str, links: dict[str, str], company_hint: str | None
    ) -> ContactInfo: ...


@runtime_checkable
class BrowserTool(Protocol):
    async def research(self, urls: list[str], questions: list[str]) -> BrowserFindings: ...


@runtime_checkable
class EmailSender(Protocol):
    async def send(self, to: str, subject: str, body: str, thread_id: str) -> SendReceipt: ...


@runtime_checkable
class CandidatePersona(Protocol):
    async def reply(
        self, profile: EnrichedProfile, thread: list[Message], disposition: Disposition
    ) -> str: ...


@runtime_checkable
class ElevenLabsClient(Protocol):
    async def create_interview(
        self, candidate_id: str, questions: list[str], context: str,
        *, job_id: str | None = None, candidate_name: str | None = None,
    ) -> InterviewSession: ...
    async def get_transcript(self, conversation_id: str) -> InterviewTranscript | None: ...
    async def find_conversation(self, candidate_id: str, since: datetime) -> str | None: ...
    def identify(self, result: Any) -> tuple[str, str] | None: ...  # (candidate_id, job_id) if token valid
    def verify_webhook(self, headers: dict, body: bytes) -> bool: ...


class FailingTool:
    """Wraps any adapter so every async method raises a transient ToolError (FAIL_TOOL demo)."""

    def __init__(self, inner: Any, name: str):
        self._inner = inner
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        target = getattr(self._inner, attr)
        if not inspect.iscoroutinefunction(target):
            return target

        @functools.wraps(target)
        async def _fail(*args: Any, **kwargs: Any) -> Any:
            raise ToolError("transient", f"FAIL_TOOL={self._name}: forced failure for demo")

        return _fail
