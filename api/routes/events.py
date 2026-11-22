"""Live activity: SSE stream + poll fallback (spec 06)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from api.contracts import EventOut
from api.db import queries
from api.routes.deps import get_conn

router = APIRouter(tags=["events"])

POLL_INTERVAL_S = 0.5


@router.get("/jobs/{job_id}/events", response_model=list[EventOut])
def list_events(job_id: str, after: int = Query(0, ge=0), conn=Depends(get_conn)):
    # Stateless poll: also re-surface delayed events that became due in the last 30s.
    due_since = queries.default_due_window() if after else None
    return queries.events_after(conn, job_id, after, due_since=due_since)


@router.get("/jobs/{job_id}/stream")
async def stream(
    job_id: str,
    request: Request,
    after: int = Query(0, ge=0),
    last_event_id: str | None = Header(None),
):
    conn = queries.connect()
    if not queries.get_job(conn, job_id):
        conn.close()
        raise HTTPException(404, "job not found")

    cursor = after
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))

    async def gen():
        nonlocal cursor
        # On reconnect, catch up on delayed events that became due while we were away.
        due_since = queries.default_due_window() if cursor else None
        sent: set[int] = set()
        try:
            while not await request.is_disconnected():
                checked_at = datetime.now(UTC)
                rows = queries.events_after(conn, job_id, cursor, due_since=due_since)
                for ev in rows:
                    if ev.id in sent:
                        continue
                    sent.add(ev.id)
                    cursor = max(cursor, ev.id)
                    yield {"id": str(ev.id), "event": "event", "data": json.dumps(ev.model_dump(mode="json"))}
                due_since = checked_at
                await asyncio.sleep(POLL_INTERVAL_S)
        finally:
            conn.close()

    return EventSourceResponse(gen(), ping=15)
