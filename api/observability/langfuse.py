"""Langfuse tracing seam (lane A; specs/05-agent-graph.md "Observability").

One trace per graph run. Tracing is best-effort and optional: without
LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY (or without the SDK installed) every call is a
no-op and `trace_url` stays None. Nothing here may ever fail a graph run.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunTrace:
    trace_id: Optional[str] = None
    url: Optional[str] = None


NOOP = RunTrace()
_client = None


def enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def _get_client():
    global _client
    if _client is None:
        from langfuse import Langfuse  # lazy: the SDK is optional

        _client = Langfuse(host=os.environ.get("LANGFUSE_HOST") or None)
    return _client


def start_run_trace(*, job_id: str, candidate_id: Optional[str], state: str,
                    event_type: str) -> RunTrace:
    """Open the `recruiter.run` trace (session_id=job_id) and return its id and URL.

    `job_id`, `candidate_id`, `state` and `event_type` become the session and tags once node
    spans are wired in (next checklist item).
    """
    if not enabled():
        return NOOP
    try:
        client = _get_client()
        trace_id = client.create_trace_id()
        return RunTrace(trace_id=trace_id, url=client.get_trace_url(trace_id=trace_id))
    except Exception:  # noqa: BLE001 - missing SDK, bad keys, network: never fail the run
        log.warning("langfuse trace unavailable; continuing untraced", exc_info=True)
        return NOOP
