"""Langfuse tracing (lane A; specs/05-agent-graph.md "Observability").

One trace per graph run (`recruiter.run`, session_id=job_id) with a span per node, a `tool` span
per tool attempt and a `generation` per LLM decide call, plus scores. Tracing is best-effort and
optional: without LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY (or without the SDK) everything is a
no-op and `trace_url` stays None. Nothing here may ever fail a graph run.

`Backend` is a thin layer over the SDK so tests can inject a fake and never touch the network.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Protocol

log = logging.getLogger(__name__)


class Handle(Protocol):
    def end(self, *, output: Any = None, error: Optional[str] = None) -> None: ...


class Backend(Protocol):
    def start_trace(self, *, name: str, session_id: str, user_id: str, tags: list[str],
                    metadata: Optional[dict] = None) -> tuple[str, Optional[str]]: ...
    def start_observation(self, *, trace_id: str, kind: str, name: str, input: Any = None,
                          model: Optional[str] = None) -> Handle: ...
    def score(self, *, trace_id: str, name: str, value: float,
              comment: Optional[str] = None) -> None: ...
    def flush(self) -> None: ...


class _SpanCtx:
    """What `with trace.span(...) as s` yields."""

    def __init__(self) -> None:
        self.output: Any = None

    def set_output(self, output: Any) -> None:
        self.output = output


@dataclass(frozen=True)
class RunTrace:
    trace_id: Optional[str] = None
    url: Optional[str] = None
    _backend: Any = field(default=None, repr=False, compare=False)

    @contextmanager
    def span(self, name: str, *, kind: str = "span", input: Any = None,
             model: Optional[str] = None) -> Iterator[_SpanCtx]:
        ctx = _SpanCtx()
        handle = None
        if self._backend is not None and self.trace_id:
            try:
                handle = self._backend.start_observation(
                    trace_id=self.trace_id, kind=kind, name=name, input=input, model=model)
            except Exception:  # noqa: BLE001 - tracing is best-effort
                log.warning("langfuse: could not open %s span", name, exc_info=True)
        error: Optional[str] = None
        try:
            yield ctx
        except BaseException as e:
            error = f"{getattr(e, 'kind', type(e).__name__)}: {e}"
            raise
        finally:
            if handle is not None:
                try:
                    handle.end(output=ctx.output, error=error)
                except Exception:  # noqa: BLE001
                    log.warning("langfuse: could not end %s span", name, exc_info=True)

    def score(self, name: str, value: float, comment: Optional[str] = None) -> None:
        if self._backend is None or not self.trace_id:
            return
        try:
            self._backend.score(trace_id=self.trace_id, name=name, value=value, comment=comment)
        except Exception:  # noqa: BLE001
            log.warning("langfuse: could not record score %s", name, exc_info=True)

    def flush(self) -> None:
        if self._backend is None:
            return
        try:
            end = getattr(self._backend, "end_trace", None)  # optional: close this run's root span
            if end is not None and self.trace_id:
                end(self.trace_id)
            self._backend.flush()
        except Exception:  # noqa: BLE001
            log.warning("langfuse: flush failed", exc_info=True)


NOOP = RunTrace()
_backend: Optional[Backend] = None


def enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


def _make_backend() -> Backend:
    return _SdkBackend()


def _get_backend() -> Backend:
    global _backend
    if _backend is None:
        _backend = _make_backend()
    return _backend


def start_run_trace(*, job_id: str, candidate_id: Optional[str], state: str,
                    event_type: str) -> RunTrace:
    """Open the `recruiter.run` trace (session_id=job_id, user_id="ceo")."""
    if not enabled():
        return NOOP
    try:
        backend = _get_backend()
        tags = [t for t in (job_id, candidate_id, state, event_type) if t]
        trace_id, url = backend.start_trace(
            name="recruiter.run", session_id=job_id, user_id="ceo", tags=tags,
            metadata={"candidate_id": candidate_id, "event_type": event_type})
        return RunTrace(trace_id=trace_id, url=url, _backend=backend)
    except Exception:  # noqa: BLE001 - missing SDK, bad keys, network: never fail the run
        log.warning("langfuse trace unavailable; continuing untraced", exc_info=True)
        return NOOP


class _SdkHandle:
    def __init__(self, obs: Any) -> None:
        self.obs = obs

    def end(self, *, output: Any = None, error: Optional[str] = None) -> None:
        if error:
            self.obs.update(output=output, level="ERROR", status_message=error)
        elif output is not None:
            self.obs.update(output=output)
        self.obs.end()


class _SdkBackend:
    """Langfuse Python SDK v3+ (OpenTelemetry based)."""

    def __init__(self) -> None:
        from langfuse import Langfuse

        self.client = Langfuse(host=os.environ.get("LANGFUSE_HOST") or None)
        self._roots: dict[str, Any] = {}  # trace_id -> root span

    def start_trace(self, *, name, session_id, user_id, tags, metadata=None):
        from langfuse import propagate_attributes

        trace_id = self.client.create_trace_id()
        # Session, user and tags are context attributes: they must be active when the root
        # span is created (spans created later in the same trace inherit them via the root).
        with propagate_attributes(user_id=user_id, session_id=session_id, tags=tags,
                                  trace_name=name):
            root = self.client.start_observation(
                trace_context={"trace_id": trace_id}, name=name, metadata=metadata)
        self._roots[trace_id] = root
        try:  # a network lookup (project id); losing the link must not lose the trace
            url = self.client.get_trace_url(trace_id=trace_id)
        except Exception:  # noqa: BLE001
            log.warning("langfuse: could not build the trace url", exc_info=True)
            url = None
        return trace_id, url

    def start_observation(self, *, trace_id, kind, name, input=None, model=None):
        root = self._roots[trace_id]
        as_type = kind if kind in ("span", "tool", "generation") else "span"
        kw: dict = {"model": model} if as_type == "generation" and model else {}
        obs = self.client.start_observation(
            trace_context={"trace_id": trace_id, "parent_span_id": root.id},
            name=name, as_type=as_type, input=input, **kw)
        return _SdkHandle(obs)

    def score(self, *, trace_id, name, value, comment=None):
        self.client.create_score(name=name, value=float(value), trace_id=trace_id,
                                 data_type="NUMERIC", comment=comment)

    def end_trace(self, trace_id: str) -> None:
        root = self._roots.pop(trace_id, None)
        if root is not None:
            root.end()

    def flush(self) -> None:
        self.client.flush()
