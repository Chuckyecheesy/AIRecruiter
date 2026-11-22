"""Outbound email (spec 02). RESTRICTED. Always simulated in the demo.

Pure: returns a SendReceipt. Lane A writes the `messages` row + contact_log and emits EmailSent.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ulid import ULID

from api.tools.base import ToolError
from api.tools.models import SendReceipt


class EmailSimulator:
    def __init__(self, allow_real_email: bool = False, allowlist: set[str] | None = None):
        self._allow_real = allow_real_email
        self._allowlist = allowlist or set()

    async def send(self, to: str, subject: str, body: str, thread_id: str) -> SendReceipt:
        if not to or "@" not in to:
            raise ToolError("invalid_input", f"bad recipient {to!r}")
        if not body.strip():
            raise ToolError("invalid_input", "empty body")
        # Hard guard: a real send is only conceivable with BOTH flags. There is no SMTP path
        # implemented at all; this exists so nobody adds one without the guard.
        if self._allow_real and to.lower() in self._allowlist:
            raise ToolError("fatal", "real email sending is not implemented in this build")
        return SendReceipt(
            message_id=f"sim_{ULID()}",
            to=to,
            thread_id=thread_id,
            simulated=True,
            sent_at=datetime.now(timezone.utc),
        )
