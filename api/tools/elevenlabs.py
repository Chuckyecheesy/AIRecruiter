"""ElevenLabs voice interview (spec 02). create = RESTRICTED, fetch transcript = READ.

The real client is the 1:40-2:50 task (needs ELEVENLABS_AGENT_ID + API docs confirmed).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ulid import ULID

from api.tools.base import ToolError
from api.tools.models import InterviewSession, InterviewTranscript, Turn

TRANSCRIPT_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "transcript.json"


class MockElevenLabsClient:
    """Dev only: the transcript becomes available `delay_s` after create_interview."""

    def __init__(self, public_base_url: str, delay_s: float = 15.0, fixture: Path = TRANSCRIPT_FIXTURE):
        self._base = public_base_url.rstrip("/")
        self._delay = delay_s
        self._fixture = json.loads(fixture.read_text())
        self._created: dict[str, float] = {}

    async def create_interview(self, candidate_id: str, questions: list[str], context: str) -> InterviewSession:
        if not questions:
            raise ToolError("invalid_input", "at least one interview question is required")
        conv_id = f"mock_conv_{ULID()}"
        self._created[conv_id] = time.monotonic()
        return InterviewSession(
            conversation_id=conv_id,
            join_url=f"{self._base}/mock-interview/{conv_id}",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
        )

    async def get_transcript(self, conversation_id: str) -> InterviewTranscript | None:
        started = self._created.get(conversation_id)
        if started is None:
            # Unknown id (e.g. after a restart): treat as done so the demo never hangs.
            started = time.monotonic() - self._delay
        if time.monotonic() - started < self._delay:
            return None
        return InterviewTranscript(
            conversation_id=conversation_id,
            turns=[Turn(**t) for t in self._fixture["turns"]],
            duration_s=self._fixture["duration_s"],
            ended_at=datetime.now(timezone.utc),
        )

    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        return True
