"""Starts the ElevenLabs/mock interview poller (lane B) against lane A's DB.

When a scheduled interview's transcript is ready, emits InterviewDone (with the transcript) for the
candidate, exactly like the webhook does. Needed in mock mode and whenever ELEVENLABS_POLL=true.
Call `start_interview_poller()` once at app startup; `stop_interview_poller()` on shutdown.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from api.config import get_settings
from api.tools import get_tools
from api.tools.elevenlabs import PendingInterview, poll_interviews

log = logging.getLogger(__name__)
_task: asyncio.Task | None = None
_stop: asyncio.Event | None = None
_job_of: dict[str, str] = {}


def _parse(ts: str | None) -> datetime:
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def _pending() -> list[PendingInterview]:
    from api.db import repo
    with repo.get_conn() as c:
        rows = c.execute(
            "SELECT i.candidate_id, i.job_id, i.conversation_id, i.created_at FROM interviews i "
            "JOIN candidates ca ON ca.id = i.candidate_id JOIN jobs j ON j.id = i.job_id "
            "WHERE ca.state = 'INTERVIEWING' AND i.status IN ('scheduled', 'in_progress') "
            "AND j.state = 'ACTIVE'").fetchall()
    out = []
    for r in rows:
        _job_of[r["candidate_id"]] = r["job_id"]
        out.append(PendingInterview(r["candidate_id"], r["conversation_id"], _parse(r["created_at"])))
    return out


def _on_done(candidate_id: str, transcript) -> None:
    from api.db import repo
    repo.emit_event(job_id=_job_of[candidate_id], type="InterviewDone", candidate_id=candidate_id,
                    payload={"conversation_id": transcript.conversation_id,
                             "transcript": transcript.model_dump(mode="json"), "source": "poller"})
    log.info("interview poller: InterviewDone for %s", candidate_id)


def _on_failed(candidate_id: str, err) -> None:
    from api.db import repo
    try:
        repo.escalate_candidate(candidate_id, f"interview failed: {err}")
    except Exception:
        log.exception("interview poller: could not escalate %s", candidate_id)


def start_interview_poller(interval_s: float | None = None) -> asyncio.Task | None:
    global _task, _stop
    s = get_settings()
    client = get_tools().elevenlabs
    if s.mode_for("elevenlabs") == "real" and not s.elevenlabs_poll and type(client).__name__ != "MockElevenLabsClient":
        log.info("interview poller: off (ELEVENLABS_POLL=false; webhook delivers transcripts)")
        return None
    if _task and not _task.done():
        return _task
    _stop = asyncio.Event()
    interval = interval_s or (3.0 if type(client).__name__ == "MockElevenLabsClient" else 10.0)

    async def get_pending():
        return await asyncio.to_thread(_pending)

    async def on_done(cid, tr):
        await asyncio.to_thread(_on_done, cid, tr)

    async def on_failed(cid, err):
        await asyncio.to_thread(_on_failed, cid, err)

    _task = asyncio.create_task(poll_interviews(client, get_pending, on_done, interval, _stop, on_failed))
    log.info("interview poller: started (every %.0fs, %s)", interval, type(client).__name__)
    return _task


async def stop_interview_poller() -> None:
    global _task
    if _stop:
        _stop.set()
    if _task:
        try:
            await asyncio.wait_for(_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _task.cancel()
    _task = None
