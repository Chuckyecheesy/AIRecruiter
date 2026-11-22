"""Event dispatcher (lane A; specs/05-agent-graph.md "Event dispatcher").

A background asyncio task: every `poll_interval` it fetches due events and runs the
handler (one graph invocation per event). Events of one candidate run serially in id
order, different candidates run concurrently under a semaphore, and the per-candidate
lock is held while the handler runs. C's main.py calls start_dispatcher/stop_dispatcher.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable, Optional

from ulid import ULID

from api.contracts import Event
from api.db import repo

log = logging.getLogger(__name__)

Handler = Callable[[Event], Awaitable[None]]
ExhaustedHook = Callable[[Event, Exception], Awaitable[None]]


class Dispatcher:
    def __init__(
        self,
        handler: Handler,
        *,
        max_concurrent: Optional[int] = None,
        poll_interval: float = 1.0,
        batch_size: int = 10,
        job_id: Optional[str] = None,
        max_attempts: int = 3,
        on_exhausted: Optional[ExhaustedHook] = None,
        owner: Optional[str] = None,
    ) -> None:
        if max_concurrent is None:
            from api.config import settings
            max_concurrent = settings.max_concurrent_runs
        self.handler = handler
        self.poll_interval = poll_interval
        self.batch_size = batch_size
        self.job_id = job_id
        self.max_attempts = max_attempts
        self.on_exhausted = on_exhausted
        self.owner = owner or f"dispatcher-{ULID()}"
        self._sem = asyncio.Semaphore(max_concurrent)

    async def run_once(self) -> int:
        """One poll cycle. Returns the number of events attempted."""
        events = await asyncio.to_thread(repo.fetch_due_events, self.batch_size, self.job_id)
        groups: dict[str, list[Event]] = defaultdict(list)
        for e in events:  # already id-ordered, so each group stays ordered
            groups[e.candidate_id or f"job:{e.job_id}"].append(e)
        counts = await asyncio.gather(*(self._run_group(g) for g in groups.values()))
        return sum(counts)

    async def _run_group(self, events: list[Event]) -> int:
        """Serial, in order. Stops at the first event that cannot or did not complete."""
        attempted = 0
        for event in events:
            cid = event.candidate_id
            if cid is not None and not await asyncio.to_thread(repo.acquire_lock, cid, self.owner):
                break  # someone else is working on this candidate; leave it pending
            try:
                if not await asyncio.to_thread(repo.claim_event, event.id):
                    break  # another dispatcher took it
                attempted += 1
                if not await self._handle(event):
                    break  # failure holds back the candidate's later events
            finally:
                if cid is not None:
                    await asyncio.to_thread(repo.release_lock, cid, self.owner)
        return attempted

    async def _handle(self, event: Event) -> bool:
        try:
            async with self._sem:
                await self.handler(event)
        except Exception as exc:  # noqa: BLE001 - any handler failure is an attempt
            log.exception("event %s (%s) failed", event.id, event.type.value)
            exhausted = await asyncio.to_thread(repo.mark_event_failed, event.id, self.max_attempts)
            if exhausted and self.on_exhausted is not None:
                try:
                    await self.on_exhausted(event, exc)
                except Exception:  # noqa: BLE001
                    log.exception("on_exhausted hook failed for event %s", event.id)
            return False
        await asyncio.to_thread(repo.mark_event_done, event.id)
        return True

    async def run_forever(self) -> None:
        while True:
            try:
                n = await self.run_once()
            except Exception:  # noqa: BLE001 - the loop must survive DB hiccups
                log.exception("dispatcher cycle failed")
                n = 0
            if n < self.batch_size:
                await asyncio.sleep(self.poll_interval)


_task: Optional[asyncio.Task] = None


def start_dispatcher(handler: Optional[Handler] = None, **kwargs) -> None:
    """Run startup recovery, then poll in a background task. No-op if already running."""
    global _task
    if _task is not None and not _task.done():
        return
    if handler is None:
        try:
            from api.agent.graph import handle_event
        except ImportError:
            log.warning("agent graph not available yet; dispatcher not started")
            return
        owner = kwargs.setdefault("owner", f"dispatcher-{ULID()}")

        async def handler(event: Event) -> None:  # the dispatcher already holds the candidate lock
            await handle_event(event, owner=owner)
    if "job_id" not in kwargs:
        from api.config import settings
        kwargs["job_id"] = settings.job_id
    n_events = repo.reset_stuck_events()
    n_locks = repo.release_stale_locks()
    if n_events or n_locks:
        log.info("crash recovery: reset %d events, released %d locks", n_events, n_locks)
    _task = asyncio.get_running_loop().create_task(Dispatcher(handler, **kwargs).run_forever())


def stop_dispatcher() -> None:
    global _task
    task, _task = _task, None
    if task is not None and not task.done():
        try:
            task.cancel()
        except RuntimeError:  # event loop already closed
            pass
