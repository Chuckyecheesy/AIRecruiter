"""Shared HTTP helpers for the real adapters: status -> ToolError mapping, safe JSON, safe path ids."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from api.tools.base import ToolError

log = logging.getLogger(__name__)
DEFAULT_RETRY_AFTER_S = 5.0


def parse_retry_after(value: str | None, default: float = DEFAULT_RETRY_AFTER_S) -> float:
    """Retry-After is either delta-seconds or an HTTP-date (RFC 9110). Never raises."""
    if not value:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, IndexError):
        return default


def raise_for_status(r: httpx.Response, tool: str) -> None:
    """Upstream bodies are logged at debug only (they can contain PII); messages carry the status."""
    if r.status_code < 400:
        return
    log.debug("%s HTTP %s body: %s", tool, r.status_code, r.text[:500])
    if r.status_code == 404:
        raise ToolError("not_found", f"{tool}: 404")
    if r.status_code == 429:
        raise ToolError("rate_limited", f"{tool}: 429", retry_after_s=parse_retry_after(r.headers.get("retry-after")))
    if r.status_code in (400, 422):
        raise ToolError("invalid_input", f"{tool}: {r.status_code} (request rejected)")
    if r.status_code in (401, 403):
        raise ToolError("fatal", f"{tool}: auth failed ({r.status_code})")
    raise ToolError("transient", f"{tool}: {r.status_code}")


def json_body(r: httpx.Response, tool: str) -> Any:
    """A 200 with a non-JSON body (proxy/HTML error page) is transient, not a crash."""
    try:
        return r.json()
    except ValueError as e:
        raise ToolError("transient", f"{tool}: non-JSON response ({r.headers.get('content-type', '?')})") from e


def path_id(value: str) -> str:
    """Encode an id for use as one URL path segment ('../admin' or 'x?a=1' can't escape it)."""
    if not value:
        raise ToolError("invalid_input", "empty id")
    return quote(str(value), safe="")
